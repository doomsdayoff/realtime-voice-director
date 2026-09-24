"""Журнал решений режиссёра: строка JSONL на каждое решение.

Вслух видна одна сторона дела — то, что бот сказал. Почему он промолчал, не видно никак:
«отказал по бюджету» и «сломался» звучат одинаково. Поэтому каждое решение пишется,
а report.py считает по журналу, где пороги стоят неправильно.

Журнал никогда не роняет речевой путь: ошибка записи выключает журнал, а не бота.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


class Journal:
    def __init__(self, path: str | Path | None = None, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.records: list[dict] = []     # без пути журнал живёт в памяти — для тестов и симулятора
        self._failed = False

    def write(self, event: str, **fields) -> None:
        if self._failed:
            return
        record = {"t": round(self.clock(), 2), "e": event}
        record.update({k: v for k, v in fields.items() if v is not None and v != ""})
        try:
            if self.path is None:
                self.records.append(record)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self._failed = True       # лучше немой журнал, чем немой бот


def read(path: str | Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # обрыв записи на последней строке не должен ломать отчёт
    return records
