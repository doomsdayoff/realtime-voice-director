"""Режиссёр эфира: единственное место, где решается, кому из слоёв голосового ИИ дать слово.

Слои (реакция на игру, разговорный слой, чат, донаты) не кладут реплику в очередь сами —
они просят разрешение, и оно выдаётся в две фазы:

    request()  — до генерации: резерв бюджета, дедупликация, право первого слова, темп;
    commit()   — перед звуком: та же проверка по свежей картине, и только тут перебивание.

Между фазами проходит 1.5–8 секунд генерации и синтеза, и за это время эфир успевает
измениться. Одна фаза — это TOCTOU: разрешение выдано под ситуацию, которой уже нет.

Модуль не генерирует текст и не играет звук. Всё, что он знает об эфире, приходит через
`Room` (что сейчас звучит и сколько секунд речи было в окне) и `Clock` — поэтому один и
тот же код работает в эфире, в тестах и в симуляторе с фальшивым временем.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .journal import Journal

Clock = Callable[[], float]


class Room(Protocol):
    """Состояние эфира, которое режиссёр читает, но не меняет (кроме перебивания)."""

    speaking: bool                  # бот сейчас издаёт звук
    current_source: str             # чей звук играет ('' — тишина)

    def speech_seconds(self, window: float) -> float:
        """Сколько секунд речи бота РЕАЛЬНО прозвучало за последние `window` секунд."""

    def quiet_for(self) -> float:
        """Сколько секунд бот молчит."""

    def barge(self) -> None:
        """Оборвать текущую реплику: преемник готов и сейчас зазвучит."""


@dataclass
class Config:
    # Бюджет — секунды речи в скользящем окне, а не число реплик: фраза на пять
    # предложений и пять коротких выкриков нагружают ухо по-разному.
    budget_sec: float = 30.0
    window_sec: float = 60.0
    guard_sec: float = 7.0             # хвост бюджета: болтовне недоступен, держится под донаты
    game_reserve_sec: float = 8.0      # доля окна, которую может взять только игровой слой
    game_sources: tuple = ("caster",)
    game_active_sec: float = 120.0     # резерв под игру держим, только пока игровой слой просит слово

    min_gap_sec: float = 5.0           # минимум тишины между репликами
    critical_score: float = 9.0        # с этого веса момент обязан прозвучать
    critical_bypasses_budget: bool = True  # False — как в первой версии: победа тонула в отказе по бюджету
    interrupt_delta: float = 1.0       # насколько весомее надо быть, чтобы оборвать говорящего
    protected_sources: tuple = ("donation",)

    # Право первого слова: стример сам среагировал на момент — бот молчит.
    owner_first: bool = True
    # 'opened_mouth' — реакция = стример ОТКРЫЛ РОТ в окне вокруг события (текущее правило).
    # 'any_speech'   — реакция = любая его речь в окне (первая версия; при сплошном
    #                  репортаже совпадение становится случайным — разбор эфира 09.09).
    owner_rule: str = "opened_mouth"
    owner_lead_sec: float = 0.4        # мог открыть рот чуть раньше, чем пришло событие
    owner_first_sec: float = 1.6       # ...и столько после события право остаётся за ним
    new_turn_gap_sec: float = 1.5      # пауза в мике, после которой речь — новая реплика
    owner_gap_sec: float = 0.6         # столько тишины в мике = фраза стримера кончилась
    patience_sec: float = 6.0          # терпеливая реплика ждёт паузу не дольше

    dedupe_sec: float = 90.0
    default_max_audio: float = 10.0
    ttl_by_source: dict = field(default_factory=lambda: {
        "caster": 9.0, "chat": 40.0, "director": 45.0, "assistant": 20.0, "donation": 600.0,
    })


class Lease:
    """Разрешение говорить. Отзывное: между выдачей и звуком мир меняется."""

    def __init__(self, arbiter: "Arbiter", *, id, source, score, reserve, granted_at, event_time,
                 latest_start, epoch_name, epoch_no, dedupe_key, interrupts, ignore_owner, patient):
        self._arb = arbiter
        self.id, self.source, self.score, self.reserve = id, source, score, reserve
        self.granted_at, self.event_time, self.latest_start = granted_at, event_time, latest_start
        self.epoch_name, self.epoch_no, self.dedupe_key = epoch_name, epoch_no, dedupe_key
        self.interrupts, self.ignore_owner, self.patient = interrupts, ignore_owner, patient
        self.committed = False
        self.closed = False
        self.cancel_reason = ""
        self.played_text: list[str] = []

    # ── проверки ──────────────────────────────────────────────────────────
    def stale_reason(self) -> str:
        """Почему разрешение больше не действует ('' — действует)."""
        arb, cfg = self._arb, self._arb.cfg
        if self.closed:
            return "закрыто"
        if self.cancel_reason:
            return self.cancel_reason
        if self.epoch_name and arb.epoch(self.epoch_name) != self.epoch_no:
            return "сменилась эпоха"
        if not self.committed and arb.now() > self.latest_start:
            return "момент протух"
        # Для терпеливой реплики речь стримера — повод подождать, а не выбросить
        if (not self.committed and cfg.owner_first and self.score < cfg.critical_score
                and not self.ignore_owner and not self.patient
                and arb.owner_reacted_to(self.event_time)):
            return "стример сказал сам"
        return ""

    def alive(self) -> bool:
        """Дёшево и без побочных эффектов: продюсеру стоит звать между предложениями,
        чтобы не синтезировать хвост реплики, которую уже некуда девать."""
        return not self.stale_reason()

    def wait_for_gap(self, sleep: Callable[[float], None] = time.sleep, max_wait: float | None = None) -> bool:
        """Терпение: дождаться паузы в речи стримера. Зовёт тот, кто говорит В РАЗГОВОР,
        и зовёт в плеере, перед самым открытием рта — от commit до звука ещё секунды."""
        arb = self._arb
        limit = arb.cfg.patience_sec if max_wait is None else max_wait
        start = arb.now()
        while arb.now() - start < limit:
            if arb.owner_quiet_for() >= arb.cfg.owner_gap_sec:
                return True
            if self.stale_reason():
                return False
            sleep(0.08)
        return arb.owner_quiet_for() >= arb.cfg.owner_gap_sec

    def commit(self) -> bool:
        """Вторая фаза: звук готов и сейчас пойдёт. Проверка по свежей картине,
        перебивание — только здесь, когда преемнику уже есть что сказать."""
        arb = self._arb
        with arb._lock:
            if self.committed:
                return True
            why = self.stale_reason()
            if why:
                self._cancel_locked(why)
                return False
            barged = bool(self.interrupts and arb.room.speaking)
            if barged:
                arb.stats["interrupted"] += 1
                arb.room.barge()
            self.committed = True
            arb._last_commit = arb.now()
            # gen — задержка выдача→звук. Если её p90 подползает к сроку годности,
            # реплики протухают ещё до первого слова (см. report.py).
            arb.journal.write("commit", id=self.id, src=self.source, score=self.score,
                              gen=round(arb.now() - self.granted_at, 2),
                              ttl=round(self.latest_start - self.event_time, 1), barge=barged)
            return True

    # ── закрытие ──────────────────────────────────────────────────────────
    def note_played(self, text: str) -> None:
        """Предложение РЕАЛЬНО прозвучало. В память эфира идёт только прозвучавшее:
        иначе бот потом сошлётся на слова, которых никто не слышал."""
        if text:
            self.played_text.append(text)

    @property
    def spoke(self) -> str:
        return " ".join(self.played_text).strip()

    def cancel(self, reason: str = "отменено") -> None:
        with self._arb._lock:
            self._cancel_locked(reason)

    def _cancel_locked(self, reason: str) -> None:
        if not self.closed:
            self.cancel_reason = reason
            self._arb.stats["cancelled"] += 1
            self._arb.journal.write("cancel", id=self.id, src=self.source, score=self.score, why=reason,
                                    waited=round(self._arb.now() - self.granted_at, 2),
                                    after_commit=self.committed)
        self._close_locked()

    def close(self) -> None:
        """Вернуть неизрасходованный резерв. Незакрытая аренда навсегда съедает бюджет —
        бот медленно и молча затыкается, поэтому удобнее всего `with lease:`."""
        with self._arb._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        arb = self._arb
        if self.closed:
            return
        self.closed = True
        arb._leases.pop(self.id, None)
        if self.committed and not self.cancel_reason:
            if self.dedupe_key:
                arb._recent_keys.append((self.dedupe_key, arb.now()))
            arb.journal.write("said", id=self.id, src=self.source, score=self.score,
                              chars=len(self.spoke), total=round(arb.now() - self.granted_at, 2))

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


class Arbiter:
    def __init__(self, room: Room, cfg: Config | None = None, clock: Clock = time.time,
                 journal: Journal | None = None):
        self.room = room
        self.cfg = cfg or Config()
        self.now = clock
        self.journal = journal or Journal(clock=clock)
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._leases: dict[int, Lease] = {}
        self._epochs: dict[str, int] = {}
        self._recent_keys: deque = deque(maxlen=64)
        self._last_commit = float("-inf")
        self._owner_turns: deque = deque(maxlen=512)   # [начало, конец] реплик стримера по VAD
        self._game_request_ts = float("-inf")
        self.stats = {"granted": 0, "denied": 0, "cancelled": 0, "interrupted": 0,
                      "by_reason": {}, "by_source": {}}
        self.journal.write("start", budget=self.cfg.budget_sec, guard=self.cfg.guard_sec,
                           gap=self.cfg.min_gap_sec, game_reserve=self.cfg.game_reserve_sec)

    # ── сигналы от стримера ───────────────────────────────────────────────
    def note_owner_voice(self) -> None:
        """Звук в мике стримера. Зовётся с VAD на каждый кадр речи (≈30 мс задержки),
        а не с расшифровки (1–2 с): только так сигнал успевает отменить реплику до звука."""
        now = self.now()
        if not self._owner_turns or now - self._owner_turns[-1][1] > self.cfg.new_turn_gap_sec:
            self._owner_turns.append([now, now])          # он открыл рот — новая реплика
            self.bump_epoch("silence", "стример заговорил")
        else:
            self._owner_turns[-1][1] = now

    def owner_reacted_to(self, event_time: float) -> bool:
        """Стример среагировал на это событие сам?

        Текущее правило — он ОТКРЫЛ РОТ в окне вокруг события. Если он говорил ещё до
        события, он говорит не про него: при сплошном репортаже «говорил рядом» —
        совпадение, а не реакция. Окно фиксировано во времени, поэтому ответ не меняется,
        пока идёт генерация, и повторная проверка в commit() не убивает реплику зря."""
        cfg = self.cfg
        lo, hi = event_time - cfg.owner_lead_sec, event_time + cfg.owner_first_sec
        if cfg.owner_rule == "any_speech":
            return any(start <= hi and end >= lo for start, end in self._owner_turns)
        return any(lo <= start <= hi for start, _ in self._owner_turns)

    def owner_quiet_for(self) -> float:
        if not self._owner_turns:
            return float("inf")
        return self.now() - self._owner_turns[-1][1]

    # ── эпохи ─────────────────────────────────────────────────────────────
    def epoch(self, name: str) -> int:
        return self._epochs.get(name, 0)

    def bump_epoch(self, name: str, why: str = "") -> int:
        """Картина принципиально изменилась (новый раунд, стример заговорил) —
        всё, что было выдано под старую эпоху, больше не к месту."""
        with self._lock:
            self._epochs[name] = self._epochs.get(name, 0) + 1
            number = self._epochs[name]
        self.journal.write("epoch", name=name, no=number, why=why)
        return number

    # ── бюджет ────────────────────────────────────────────────────────────
    def _budget_used(self) -> float:
        """Прозвучавшее + резерв под все открытые разрешения. Резерв держится до закрытия:
        иначе у звучащей реплики недосчитан хвост, и три слоя дружно пролезут в остаток."""
        reserved = sum(lease.reserve for lease in self._leases.values())
        return self.room.speech_seconds(self.cfg.window_sec) + reserved

    def _cap_for(self, source: str) -> float:
        cfg = self.cfg
        cap = cfg.budget_sec - cfg.guard_sec
        # Разговор просит слово втрое чаще игры и выгребает окно первым, а реакция на
        # игру приходит не когда удобно. Кусок окна держим под игру — пока она идёт.
        if source not in cfg.game_sources and self.now() - self._game_request_ts < cfg.game_active_sec:
            cap -= cfg.game_reserve_sec
        return cap

    def budget_free(self, source: str) -> float:
        """Сколько секунд речи слой может взять прямо сейчас. Нужен слоям, которые сами
        выбирают длину реплики: резерв обязан совпадать с тем, что реально прозвучит."""
        with self._lock:
            return max(0.0, self._cap_for(source) - self._budget_used())

    # ── первая фаза ───────────────────────────────────────────────────────
    def request(self, source: str, score: float = 5.0, *, event_time: float | None = None,
                ttl: float | None = None, max_audio_s: float | None = None, dedupe_key: str | None = None,
                epoch: str | None = None, interrupts: bool | None = None, ignore_owner: bool = False,
                patient: bool = False) -> Lease | None:
        """Можно ли начинать думать над репликой. Вернёт Lease или None (и причину — в журнал).

        score        0..10, вес момента; критический (>= critical_score) обязан прозвучать.
        event_time   когда СЛУЧИЛОСЬ событие: от него считаются срок годности и право первого слова.
        max_audio_s  сколько секунд резервировать; должно совпадать с реальной длиной реплики.
        epoch        к какой эпохе привязана уместность ('silence', 'round').
        patient      реплика в разговор: речь стримера её не отменяет, а откладывает до паузы.
        """
        cfg = self.cfg
        now = self.now()
        event_time = now if event_time is None else event_time
        max_audio_s = cfg.default_max_audio if max_audio_s is None else max_audio_s
        critical = score >= cfg.critical_score
        if interrupts is None:
            interrupts = critical

        with self._lock:
            if source in cfg.game_sources:
                self._game_request_ts = now

            # 1. Дубль темы — дешевле всего и раздражает сильнее всего.
            if dedupe_key and any(k == dedupe_key and now - ts < cfg.dedupe_sec for k, ts in self._recent_keys):
                return self._deny("уже говорили об этом", source, score)

            # 2. Право первого слова. Проверяется и здесь (не тратить модель), и в commit().
            if (cfg.owner_first and not critical and not ignore_owner and not patient
                    and self.owner_reacted_to(event_time)):
                return self._deny("стример комментирует сам", source, score)

            # 3. Бюджет. Критическое событие бюджет не проверяет: победа в катке обязана прозвучать.
            if not critical or not cfg.critical_bypasses_budget:
                cap = self._cap_for(source)
                if self._budget_used() + max_audio_s > cap:
                    return self._deny("бюджет речи", source, score, cap=round(cap, 1), need=round(max_audio_s, 1))

            # 4. Темп: бюджет не спасает от пяти коротких выкриков подряд.
            if not critical and self.room.quiet_for() < cfg.min_gap_sec:
                return self._deny("рано после прошлой реплики", source, score)

            # 5. Бот уже говорит. Донат не перебивается ничем: это деньги зрителя.
            if self.room.speaking:
                if self.room.current_source in cfg.protected_sources:
                    return self._deny("играет донат", source, score)
                current = max((lease.score for lease in self._leases.values() if lease.committed), default=0.0)
                if not (interrupts and score >= current + cfg.interrupt_delta):
                    return self._deny("бот сейчас говорит", source, score)

            lease = Lease(self, id=next(self._ids), source=source, score=score, reserve=max_audio_s,
                          granted_at=now, event_time=event_time,
                          latest_start=event_time + (ttl if ttl is not None else cfg.ttl_by_source.get(source, 20.0)),
                          epoch_name=epoch, epoch_no=self.epoch(epoch) if epoch else 0, dedupe_key=dedupe_key,
                          interrupts=interrupts, ignore_owner=ignore_owner, patient=patient)
            self._leases[lease.id] = lease
            self.stats["granted"] += 1
            self.stats["by_source"][source] = self.stats["by_source"].get(source, 0) + 1
            self.journal.write("grant", id=lease.id, src=source, score=score, reserve=round(max_audio_s, 1),
                               hold=round(lease.latest_start - now, 1), patient=patient or None,
                               used=round(self._budget_used() - max_audio_s, 1))
            return lease

    def _deny(self, reason: str, source: str, score: float, **extra) -> None:
        self.stats["denied"] += 1
        self.stats["by_reason"][reason] = self.stats["by_reason"].get(reason, 0) + 1
        self.journal.write("deny", src=source, score=score, why=reason,
                           used=round(self.room.speech_seconds(self.cfg.window_sec), 1), **extra)
        return None

    # ── служебное ─────────────────────────────────────────────────────────
    def open_leases(self) -> int:
        """Сколько разрешений выдано и не закрыто. После холостого прогона должно быть 0."""
        return len(self._leases)

    def cancel_all(self, reason: str = "стоп") -> None:
        with self._lock:
            leases = list(self._leases.values())
        for lease in leases:
            lease.cancel(reason)
