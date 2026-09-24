"""Прогон одного сценария эфира через политику выдачи слова, с фальшивым временем.

Шаг — 0.1 с. На каждом шаге: VAD стримера, новые события, готовые к звуку реплики,
плеер с одной «глоткой». Политики различаются только тем, как они решают, кому
говорить; конвейер «сгенерировать → дождаться звука → проиграть» у всех общий.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from director import Arbiter, Config, Journal

from .scenario import CHARS_PER_SEC, Scenario

DT = 0.1
DRAIN_SEC = 90.0                 # после конца эфира доигрываем то, что уже начато
BARGE_PAUSE = 0.16               # пауза между обрывом и новым голосом
DONATION_SYNTH = 2.5             # синтез интро доната
CASTER_TTL = 9.0


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Segment:
    start: float
    end: float
    source: str
    job: "Job"


@dataclass
class Job:
    source: str
    score: float
    event_time: float
    ready_at: float
    seconds: float
    priority: int
    lease: object = None
    wait_started: float | None = None
    started_at: float | None = None
    committed: bool = False


class SimRoom:
    """Эфир глазами режиссёра: что звучит и сколько секунд речи было в окне."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.segments: list[Segment] = []
        self.current: Segment | None = None
        self.barges: list[float] = []

    @property
    def speaking(self) -> bool:
        return self.current is not None and self.clock() < self.current.end

    @property
    def current_source(self) -> str:
        return self.current.source if self.speaking else ""

    def speech_seconds(self, window: float) -> float:
        now = self.clock()
        lo = now - window
        return sum(max(0.0, min(s.end, now) - max(s.start, lo)) for s in self.segments[-40:] if s.end > lo)

    def quiet_for(self) -> float:
        if self.speaking:
            return 0.0
        if not self.segments:
            return float("inf")
        return self.clock() - max(s.end for s in self.segments[-5:])

    def barge(self) -> None:
        if self.speaking:
            self.current.end = self.clock()
            self.barges.append(self.clock())

    def start(self, job: Job, delay: float = 0.0) -> Segment:
        begin = self.clock() + delay
        segment = Segment(begin, begin + job.seconds, job.source, job)
        job.started_at = begin
        self.segments.append(segment)
        self.current = segment
        return segment


@dataclass
class Result:
    policy: str
    seed: int
    events: int = 0
    critical_events: int = 0
    caster_voiced: int = 0
    caster_voiced_critical: int = 0
    caster_stale: int = 0              # началась позже срока годности события
    caster_delays: list = field(default_factory=list)
    director_lines: int = 0
    bot_seconds: float = 0.0
    caster_seconds: float = 0.0
    director_seconds: float = 0.0
    started_over_owner: int = 0        # реплика началась, когда стример говорил (донаты и критические не считаем)
    back_to_back: int = 0              # реплика вплотную к прошлой (< 2 с тишины) — «болтовня пачками»
    dead_air_sec: float = 0.0          # тишина между обрывом и новым голосом
    open_leases: int = 0
    deny: dict = field(default_factory=dict)
    duration: float = 0.0


class Policy:
    """Общая часть: очередь готовых реплик и плеер. Наследники решают, кому давать слово."""

    name = "base"

    def __init__(self, scenario: Scenario):
        self.sc = scenario
        self.clock = FakeClock()
        self.room = SimRoom(self.clock)
        self.pending: list[Job] = []
        self.resume_at = 0.0
        self.owner_on = False
        self.last_owner_voice = float("-inf")

    # ── события, которые переопределяют политики ──────────────────────────
    def on_owner_voice(self) -> None:
        self.last_owner_voice = self.clock()

    def on_game_event(self, event) -> None: ...

    def on_talk_attempt(self, gen: float) -> None: ...

    def on_donation(self, donation) -> None:
        self.pending.append(Job("donation", 10.0, self.clock(), self.clock() + DONATION_SYNTH,
                                donation.seconds, priority=0))

    def can_start(self, job: Job) -> str:
        """'start' | 'wait' | 'drop' для реплики во главе очереди при свободном эфире."""
        return "start"

    def try_barge(self, ready: list[Job]) -> None:
        """Эфир занят, а в очереди есть готовое. Может ли кто-то перебить."""

    def finished(self, job: Job, interrupted: bool) -> None: ...

    def hand_over(self, job: Job) -> None:
        """Оборванная реплика закрывается ДО того, как зазвучит преемник: иначе её аренда
        не закроется никогда и резерв навсегда съест бюджет."""
        cut = self.room.current
        if cut is not None and cut.job is not job:
            self.finished(cut.job, interrupted=True)
        self.room.start(job, delay=BARGE_PAUSE)

    # ── плеер ─────────────────────────────────────────────────────────────
    def step(self) -> None:
        now = self.clock()
        current = self.room.current
        if current is not None and now >= current.end:
            self.finished(current.job, interrupted=current.end < current.start + current.job.seconds - 1e-6)
            self.room.current = None
        ready = sorted((j for j in self.pending if j.ready_at <= now), key=lambda j: (j.priority, j.ready_at))
        if not ready:
            return
        if self.room.speaking:
            self.try_barge(ready)
            return
        if now < self.resume_at:
            return
        for job in ready:
            verdict = self.can_start(job)
            if verdict == "start":
                self.pending.remove(job)
                self.room.start(job)
                return
            if verdict == "wait":          # одна «глотка»: ждущая реплика держит очередь
                return
            self.pending.remove(job)       # drop


def run(policy: Policy) -> Result:
    sc = policy.sc
    res = Result(policy=policy.name, seed=sc.rng_seed, duration=sc.duration, events=len(sc.events),
                 critical_events=sum(e.score >= 9 for e in sc.events))
    turns, events, donations, attempts = sc.owner_turns, sc.events, sc.donations, sc.talk_attempts
    ti = ei = di = ai = 0
    steps = int((sc.duration + DRAIN_SEC) / DT)
    for step in range(steps):
        t = step * DT
        policy.clock.t = t
        while ti < len(turns) and turns[ti][1] < t:
            ti += 1
        if ti < len(turns) and turns[ti][0] <= t <= turns[ti][1]:
            policy.on_owner_voice()
        if t <= sc.duration:
            while ei < len(events) and events[ei].t <= t:
                policy.on_game_event(events[ei])
                ei += 1
            while di < len(donations) and donations[di].t <= t:
                policy.on_donation(donations[di])
                di += 1
            while ai < len(attempts) and attempts[ai][0] <= t:
                policy.on_talk_attempt(attempts[ai][1])
                ai += 1
        policy.step()
    return collect(policy, res)


def collect(policy: Policy, res: Result) -> Result:
    turns = policy.sc.owner_turns
    for seg in policy.room.segments:
        length = max(0.0, seg.end - seg.start)
        res.bot_seconds += length
        if seg.source == "caster":
            res.caster_seconds += length
            res.caster_voiced += 1
            res.caster_voiced_critical += seg.job.score >= 9
            res.caster_delays.append(seg.start - seg.job.event_time)
            res.caster_stale += seg.start - seg.job.event_time > CASTER_TTL
        elif seg.source == "director":
            res.director_seconds += length
            res.director_lines += 1
        if seg.source != "donation" and seg.job.score < 9:
            res.started_over_owner += any(a < seg.start < b for a, b in turns)
    ordered = sorted(policy.room.segments, key=lambda s: s.start)
    for prev, seg in zip(ordered, ordered[1:]):
        if seg.source != "donation" and prev.source != "donation" and 0 <= seg.start - prev.end < 2.0:
            res.back_to_back += 1
    starts = sorted(s.start for s in policy.room.segments)
    for barge_t in policy.room.barges:
        nxt = next((s for s in starts if s >= barge_t), None)
        if nxt is not None:
            res.dead_air_sec += nxt - barge_t
    return res


# ── политики ────────────────────────────────────────────────────────────────


class NaiveGates(Policy):
    """Как было до режиссёра: у каждого слоя свой кулдаун, никто не знает про других.

    Кастер: кулдаун 12 с и «уже говорю — дропаю». Разговорный слой: 40 с с прошлой своей
    реплики и тишина у стримера по расшифровке (она отстаёт на 1.5 с). Готовая реплика
    играет, когда освободится эфир, — без срока годности и без повторной проверки.
    Донат обрывает эфир в момент прихода, а его звук готов только через 2.5 с.
    """

    name = "независимые кулдауны"

    def __init__(self, scenario):
        super().__init__(scenario)
        self.last_caster = float("-inf")
        self.last_director_end = float("-inf")

    def on_game_event(self, ev):
        now = self.clock()
        if self.room.speaking or now - self.last_caster < 12.0:
            return
        self.last_caster = now
        self.pending.append(Job("caster", ev.score, ev.t, now + ev.gen,
                                17.0 if ev.deep else 8.0, priority=5))

    def on_talk_attempt(self, gen):
        now = self.clock()
        heard_quiet = now - 1.5 - self.last_owner_voice > 1.0      # по расшифровке, с опозданием
        if any(j.source == "director" for j in self.pending) or now - self.last_director_end < 40.0:
            return
        if self.room.quiet_for() < 5.0 or not heard_quiet:
            return
        self.pending.append(Job("director", 5.0, now, now + gen, 227 / CHARS_PER_SEC, priority=7))

    def on_donation(self, donation):
        self.room.barge()                  # флаг «пришёл донат» — рубим сразу
        super().on_donation(donation)

    def finished(self, job, interrupted):
        if job.source == "director":
            self.last_director_end = self.clock()


class Director(Policy):
    """Режиссёр эфира: две фазы разрешения, бюджет в секундах, эпохи, право первого слова."""

    name = "режиссёр"
    # Резерв кастера и разговорного слоя: что слой ЗАЯВЛЯЕТ и сколько РЕАЛЬНО звучит
    caster_reserve = {False: 8.0, True: 17.0}
    director_ladder = (17.5, 12.0, 7.0)    # длина реплики выбирается под остаток бюджета
    director_fixed_reserve = None          # None — резерв честный; число — «врущий» резерв
    director_real_seconds = None
    game_waits_for_gap = True              # реакция на игру ждёт паузу стримера в плеере

    def __init__(self, scenario, cfg: Config | None = None):
        super().__init__(scenario)
        self.journal = Journal(clock=self.clock)
        self.arb = Arbiter(self.room, cfg or self.config(), clock=self.clock, journal=self.journal)

    @staticmethod
    def config() -> Config:
        return Config(budget_sec=42.0, guard_sec=7.0, game_reserve_sec=8.0,
                      owner_rule="opened_mouth", critical_bypasses_budget=True)

    def on_owner_voice(self):
        super().on_owner_voice()
        self.arb.note_owner_voice()

    def on_game_event(self, ev):
        lease = self.arb.request("caster", ev.score, event_time=ev.t, ttl=CASTER_TTL,
                                 max_audio_s=self.caster_reserve[ev.deep])
        if lease:
            self.pending.append(Job("caster", ev.score, ev.t, self.clock() + ev.gen,
                                    17.0 if ev.deep else 8.0, priority=5, lease=lease))

    def on_talk_attempt(self, gen):
        if any(j.source == "director" for j in self.pending) or self.room.current_source == "director":
            return
        if self.director_fixed_reserve is not None:
            reserve, seconds = self.director_fixed_reserve, self.director_real_seconds
        else:
            free = self.arb.budget_free("director")
            reserve = next((step for step in self.director_ladder if step <= free), self.director_ladder[-1])
            seconds = reserve
        lease = self.arb.request("director", 5.0, max_audio_s=reserve, patient=True)
        if lease:
            self.pending.append(Job("director", 5.0, self.clock(), self.clock() + gen, seconds,
                                    priority=7, lease=lease))

    def can_start(self, job):
        lease = job.lease
        if lease is None:                                   # донат идёт мимо аренд
            return "start"
        cfg, now = self.arb.cfg, self.clock()
        # Реакция на игру ждёт паузу стримера, критическая — нет: победа в катке обязана прозвучать
        waits = lease.patient or (job.source == "caster" and self.game_waits_for_gap
                                  and job.score < cfg.critical_score)
        if waits and self.arb.owner_quiet_for() < cfg.owner_gap_sec:
            job.wait_started = job.wait_started or now
            if lease.stale_reason() or now - job.wait_started > cfg.patience_sec:
                lease.cancel("стример так и не замолчал")
                return "drop"
            return "wait"
        if job.committed or lease.commit():
            job.committed = True
            return "start"
        return "drop"

    def try_barge(self, ready):
        now = self.clock()
        for job in ready:
            if job.source == "donation" and self.room.current_source != "donation":
                # Перебивание в тот миг, когда звук доната ГОТОВ, а не когда донат пришёл
                self.room.barge()
                self.pending.remove(job)
                self.hand_over(job)
                return
            lease = job.lease
            if lease is not None and lease.interrupts and not job.committed:
                if lease.commit():                           # внутри commit — room.barge()
                    job.committed = True
                    self.pending.remove(job)
                    self.hand_over(job)
                else:
                    self.pending.remove(job)
                return

    def finished(self, job, interrupted):
        if job.lease is not None:
            job.lease.note_played("…")
            job.lease.close()


class DirectorV1(Director):
    """Первая версия режиссёра, как она стояла в эфире 09.09.2026.

    Три поломки, найденные потом по журналу: разговорный слой резервировал 7 с, а говорил
    17.5; право первого слова срабатывало на любую речь стримера рядом с событием; у игры
    не было своей доли бюджета, и критическое событие тоже проверялось бюджетом.
    """

    name = "режиссёр v1"
    caster_reserve = {False: 5.0, True: 13.0}        # занижено против реальных 8/17
    director_fixed_reserve = 7.0
    director_real_seconds = 227 / CHARS_PER_SEC      # ≈17.5 с
    game_waits_for_gap = False

    @staticmethod
    def config() -> Config:
        return Config(budget_sec=22.0, guard_sec=7.0, game_reserve_sec=0.0,
                      owner_rule="any_speech", critical_bypasses_budget=False)


def finalize(policy: Policy, res: Result) -> Result:
    if isinstance(policy, Director):
        for job in list(policy.pending):                 # всё, что не дождалось звука
            if job.lease is not None:
                job.lease.cancel("эфир кончился")
        res.open_leases = policy.arb.open_leases()
        deny: dict = {}
        for rec in policy.journal.records:
            if rec["e"] == "deny":
                deny.setdefault(rec["src"], {}).setdefault(rec["why"], 0)
                deny[rec["src"]][rec["why"]] += 1
        res.deny = deny
    return res


POLICIES = (NaiveGates, DirectorV1, Director)


def simulate(scenario_factory, seed: int, policy_cls) -> Result:
    policy = policy_cls(scenario_factory(seed))
    return finalize(policy, run(policy))
