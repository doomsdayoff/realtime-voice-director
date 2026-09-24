"""Потоковая озвучка: токены модели → предложения → синтез наперёд → звук по порядку.

Без потока бот ждёт всю реплику от модели, синтезирует её целиком и только потом
говорит — в живом эфире это 6.8–7.8 с от разрешения до первого звука. С потоком первое
предложение звучит, пока пишется второе: остаётся время до первого токена и синтез
одного предложения.

Вторая фаза разрешения (`commit`) случается ровно в момент, когда готов звук первого
предложения. Перебивание — тоже в этот момент: обрывать чужую речь раньше, чем преемнику
есть что сказать, значит оставить эфир в тишине на время синтеза.
"""
from __future__ import annotations

import queue
import re
import threading
from typing import Callable, Iterable, Iterator

from .arbiter import Lease

# Конец предложения — знак и пробел после него: «3.5» и «v2.1» не режутся.
_SENTENCE_END = re.compile(r"[.!?…]+[»\")]*\s+|\n+")


def split_sentences(chunks: Iterable[str]) -> Iterator[str]:
    """Режет поток кусков текста на предложения по мере поступления."""
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while True:
            match = _SENTENCE_END.search(buffer)
            if not match:
                break
            sentence, buffer = buffer[:match.end()].strip(), buffer[match.end():]
            if sentence:
                yield sentence
    tail = buffer.strip()
    if tail:
        yield tail


def speak(lease: Lease, chunks: Iterable[str], synthesize: Callable[[str], object],
          play: Callable[[object], bool], *, sleep: Callable[[float], None] | None = None) -> str:
    """Проговорить реплику по разрешению. Вернёт то, что реально прозвучало.

    synthesize(text) -> audio   синтез одного предложения (идёт в фоне, наперёд);
    play(audio) -> bool         проиграть; False — звук оборвали (стоп, перебили).
    """
    ready: queue.Queue = queue.Queue()

    def producer() -> None:
        try:
            for sentence in split_sentences(chunks):
                if not lease.alive():             # незачем синтезировать хвост реплики, которую некуда девать
                    break
                ready.put((sentence, synthesize(sentence)))
        finally:
            ready.put(None)

    worker = threading.Thread(target=producer, daemon=True)
    worker.start()
    try:
        first = ready.get()
        if first is None:
            return ""
        # Терпеливая реплика ждёт паузу в речи стримера в плеере, перед самым звуком:
        # от выдачи слова досюда прошли секунды генерации, отказывать раньше — рано.
        if lease.patient and not lease.wait_for_gap(**({"sleep": sleep} if sleep else {})):
            lease.cancel("стример так и не замолчал")
            return ""
        if not lease.commit():
            return ""
        item = first
        while item is not None:
            sentence, audio = item
            # Начатую реплику срок годности не рубит: оборванная на полуслове мысль
            # звучит хуже запоздавшей. Рубит только явный обрыв звука.
            if not play(audio):
                break
            lease.note_played(sentence)
            item = ready.get()
        return lease.spoke
    finally:
        lease.close()
        worker.join(timeout=5)
