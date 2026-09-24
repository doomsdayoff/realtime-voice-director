"""Синтетический эфир: стример, игровые события, донаты — с фиксированным сидом.

Параметры профиля взяты из журналов живого эфира 09.09.2026 (Дота, 99 минут), а не
придуманы: 115 игровых поводов, новая реплика стримера раз в ~12 секунд, разговорный
слой просит слово раз в ~17 секунд, медиана реплики разговорного слоя — 227 символов.
Сами события и их время — случайные: это модель эфира, а не его запись.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

CHARS_PER_SEC = 13.0                    # темп речи TTS, замер по 185 репликам


@dataclass
class GameEvent:
    t: float
    score: float
    deep: bool                           # разбор (≈17 с), а не выкрик (≈8 с)
    gen: float                           # от решения говорить до готового звука первого предложения


@dataclass
class Donation:
    t: float
    seconds: float


@dataclass
class Scenario:
    duration: float
    owner_turns: list[tuple[float, float]]
    events: list[GameEvent]
    donations: list[Donation]
    talk_attempts: list[tuple[float, float]]   # (когда разговорному слою есть что сказать, его gen)
    rng_seed: int
    extra: dict = field(default_factory=dict)


# Вес события 0..10: основная масса — рядовые поводы, критических (≥9) единицы
SCORE_WEIGHTS = {5: 25, 6: 30, 7: 25, 8: 13, 9: 5, 10: 2}


def dota_reportage(seed: int, minutes: float = 99.0, events: int = 115) -> Scenario:
    rng = random.Random(seed)
    duration = minutes * 60.0

    # Стример ведёт репортаж о себе: реплика 3–9 с, новая — в среднем раз в 12 с
    turns, t = [], rng.uniform(0, 5)
    while t < duration:
        length = rng.uniform(3.0, 9.0)
        turns.append((t, min(t + length, duration)))
        t += max(length + 1.6, rng.expovariate(1 / 12.0))

    scores, weights = zip(*SCORE_WEIGHTS.items())
    game = []
    for _ in range(events):
        deep = rng.random() < 0.4
        # быстрая модель на выкрик, медленная с рассуждением — на разбор
        gen = rng.uniform(4.0, 8.0) if deep else rng.uniform(1.5, 3.5)
        game.append(GameEvent(t=rng.uniform(30, duration - 30), score=float(rng.choices(scores, weights)[0]),
                              deep=deep, gen=gen))
    game.sort(key=lambda e: e.t)

    donations, t = [], rng.expovariate(1 / 600.0)
    while t < duration:
        donations.append(Donation(t=t, seconds=rng.uniform(10.0, 20.0)))
        t += rng.expovariate(1 / 600.0)

    attempts, t = [], rng.expovariate(1 / 17.0)
    while t < duration:
        attempts.append((t, rng.uniform(3.0, 8.0)))
        t += rng.expovariate(1 / 17.0)

    return Scenario(duration=duration, owner_turns=turns, events=game, donations=donations,
                    talk_attempts=attempts, rng_seed=seed)
