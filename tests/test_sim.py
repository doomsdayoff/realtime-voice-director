"""Симулятор: модель должна повторять то, что было видно по журналам живого эфира."""
from pathlib import Path
from statistics import median

import pytest

from sim import run as sim_run
from sim.engine import Director, DirectorV1, NaiveGates, finalize, run
from sim.scenario import dota_reportage

SEEDS = (1, 2, 3)


def results(cls):
    out = []
    for seed in SEEDS:
        policy = cls(dota_reportage(seed))
        out.append(finalize(policy, run(policy)))
    return out


@pytest.fixture(scope="module")
def v1():
    return results(DirectorV1)


@pytest.fixture(scope="module")
def current():
    return results(Director)


def test_no_leaked_leases(v1, current):
    assert all(r.open_leases == 0 for r in v1 + current)


def test_first_version_reproduces_the_stream_of_0909(v1):
    """В живом эфире 09.09 кастер озвучил 5–6 поводов из 115, а отказы делили
    «бюджет речи» и «стример комментирует сам». Модель на старых правилах должна
    повторять эту картину, иначе её числам про новые правила верить нельзя."""
    assert median(r.caster_voiced for r in v1) <= 15
    for r in v1:
        top = sorted(r.deny["caster"].items(), key=lambda kv: -kv[1])[:2]
        assert {why for why, _ in top} == {"бюджет речи", "стример комментирует сам"}


def test_current_rules_give_the_game_its_voice_back(v1, current):
    assert median(r.caster_voiced for r in current) >= 3 * median(r.caster_voiced for r in v1)
    assert median(100 * r.caster_voiced_critical / max(r.critical_events, 1) for r in current) >= 75


def test_director_does_not_start_over_the_streamer():
    naive = results(NaiveGates)
    assert median(r.started_over_owner for r in naive) >= 20
    assert all(r.started_over_owner <= 2 for r in results(Director))


def test_committed_summary_is_what_the_code_produces():
    committed = (Path(sim_run.RESULTS) / "summary.md").read_text(encoding="utf-8")
    assert sim_run.summary_markdown(sim_run.simulate_all()) == committed
