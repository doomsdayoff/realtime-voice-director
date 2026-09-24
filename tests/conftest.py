import pytest

from director import Arbiter, Config, Journal


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


class FakeRoom:
    """Эфир для тестов: что звучит и сколько речи было — задаётся руками."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.speaking = False
        self.current_source = ""
        self.spoken: list[tuple[float, float]] = []   # (начало, конец) прозвучавшего
        self.barged = 0

    def speech_seconds(self, window: float) -> float:
        lo, now = self.clock() - window, self.clock()
        return sum(max(0.0, min(b, now) - max(a, lo)) for a, b in self.spoken)

    def quiet_for(self) -> float:
        if self.speaking:
            return 0.0
        return self.clock() - max((b for _, b in self.spoken), default=float("-inf"))

    def barge(self) -> None:
        self.barged += 1

    def say(self, seconds: float, source: str = "director") -> None:
        """Проиграть реплику: время идёт, секунды попадают в окно бюджета."""
        start = self.clock()
        self.speaking, self.current_source = True, source
        self.clock.tick(seconds)
        self.spoken.append((start, self.clock()))
        self.speaking, self.current_source = False, ""


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def room(clock):
    return FakeRoom(clock)


@pytest.fixture
def make(room, clock):
    def _make(**overrides) -> Arbiter:
        return Arbiter(room, Config(**overrides), clock=clock, journal=Journal(clock=clock))
    return _make
