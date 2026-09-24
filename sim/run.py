"""Прогон симулятора: три политики на одних и тех же 10 синтетических эфирах.

    python -m sim.run        # results/summary.md, results/report_director.txt, charts/*.png
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median

from director.report import render, summarize

from .engine import POLICIES, Director, DirectorV1, NaiveGates, finalize, run
from .scenario import dota_reportage

ROOT = Path(__file__).resolve().parent.parent
RESULTS, CHARTS = ROOT / "results", ROOT / "charts"
SEEDS = range(1, 11)

METRICS = [
    ("Игровых поводов озвучено (из 115)", lambda r: r.caster_voiced),
    ("Критических озвучено, %", lambda r: 100 * r.caster_voiced_critical / max(r.critical_events, 1)),
    ("Реплик кастера протухло до звука", lambda r: r.caster_stale),
    ("Реплик разговорного слоя", lambda r: r.director_lines),
    ("Эфира бота, %", lambda r: 100 * r.bot_seconds / r.duration),
    ("Доля игры в речи бота, %", lambda r: 100 * r.caster_seconds / max(r.bot_seconds, 1)),
    ("Начал реплику поверх стримера", lambda r: r.started_over_owner),
    ("Тишина после обрыва, с", lambda r: r.dead_air_sec),
    ("Незакрытых разрешений в конце", lambda r: r.open_leases),
]


def simulate_all():
    results = {}
    for cls in POLICIES:
        runs = []
        for seed in SEEDS:
            policy = cls(dota_reportage(seed))
            runs.append(finalize(policy, run(policy)))
        results[cls.name] = runs
    return results


def cell(values):
    lo, mid, hi = min(values), median(values), max(values)
    return f"{mid:.0f}" if lo == hi else f"{mid:.0f} ({lo:.0f}–{hi:.0f})"


def summary_markdown(results) -> str:
    names = list(results)
    lines = ["# Симуляция: три политики на одних и тех же эфирах", "",
             f"Сценарий — репортаж о Доте на 99 минут, 115 игровых поводов, {len(SEEDS)} сидов. "
             "Медиана, в скобках — разброс по сидам.", "",
             "| Метрика | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    for title, metric in METRICS:
        lines.append(f"| {title} | " + " | ".join(cell([metric(r) for r in results[n]]) for n in names) + " |")
    lines += ["", "## Почему молчал кастер (сумма по сидам)", ""]
    for name in names[1:]:
        reasons: dict = {}
        for r in results[name]:
            for why, n in r.deny.get("caster", {}).items():
                reasons[why] = reasons.get(why, 0) + n
        total = sum(reasons.values())
        top = ", ".join(f"{why} — {n}" for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]))
        lines.append(f"- **{name}**: {total} отказов: {top}")
    return "\n".join(lines) + "\n"


def draw(results, sample_policies):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    names = list(results)
    colors = ["#adb5bd", "#f08c00", "#1c7ed6"]
    panels = [METRICS[0], METRICS[1], METRICS[6], METRICS[4]]

    fig, axes = plt.subplots(1, len(panels), figsize=(15, 4))
    for ax, (title, metric) in zip(axes, panels):
        mids, errs = [], [[], []]
        for name in names:
            values = [metric(r) for r in results[name]]
            mid = median(values)
            mids.append(mid)
            errs[0].append(mid - min(values))
            errs[1].append(max(values) - mid)
        ax.bar(range(len(names)), mids, color=colors, yerr=errs, capsize=4, ecolor="#495057")
        for i, v in enumerate(mids):
            ax.text(i + 0.12, v + errs[1][i], f"{v:.0f}", ha="left", va="bottom", fontsize=9)
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels([n.replace(" ", "\n", 1) for n in names], fontsize=8)
        ax.set_title(title, fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(CHARTS / "policies.png", dpi=150)
    plt.close(fig)

    # Лента эфира: 6 минут одного сида — там, где версии расходятся сильнее всего. Это
    # иллюстрация разницы, а не типичный отрезок: типичные цифры — в summary.md
    def voiced_in(policy, a, b):
        return sum(1 for seg in policy.room.segments if seg.source == "caster" and a <= seg.job.event_time <= b)
    span = 360.0
    start = max(range(60, int(sample_policies[0].sc.duration - span), 30),
                key=lambda a: voiced_in(sample_policies[-1], a, a + span) - voiced_in(sample_policies[1], a, a + span))
    start, end = float(start), float(start) + span
    fig, axes = plt.subplots(len(sample_policies), 1, figsize=(15, 2.2 * len(sample_policies)), sharex=True)
    source_colors = {"caster": "#1c7ed6", "director": "#f08c00", "donation": "#2f9e44"}
    for ax, policy in zip(axes, sample_policies):
        for a, b in policy.sc.owner_turns:
            if b > start and a < end:
                ax.barh(1, b - a, left=a, height=0.5, color="#ced4da")
        for seg in policy.room.segments:
            if seg.end > start and seg.start < end:
                ax.barh(0, seg.end - seg.start, left=seg.start, height=0.5,
                        color=source_colors.get(seg.source, "#868e96"))
        voiced = {round(s.job.event_time, 2) for s in policy.room.segments if s.source == "caster"}
        for ev in policy.sc.events:
            if start <= ev.t <= end:
                hit = round(ev.t, 2) in voiced
                ax.plot(ev.t, -0.55, marker="^" if hit else "x", color="#1c7ed6" if hit else "#e03131",
                        markersize=8 if ev.score >= 9 else 6)
        ax.set_yticks([1, 0])
        ax.set_yticklabels(["стример", "бот"])
        ax.set_ylim(-0.9, 1.5)
        ax.set_title(policy.name, loc="left", fontsize=10)
    axes[-1].set_xlim(start, end)
    axes[-1].set_xticks(range(int(start), int(end) + 1, 60))
    axes[-1].set_xticklabels([f"{int(t // 60)}:{int(t % 60):02d}" for t in range(int(start), int(end) + 1, 60)])
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in source_colors.values()]
    handles += [plt.Line2D([], [], marker="^", color="#1c7ed6", linestyle=""),
                plt.Line2D([], [], marker="x", color="#e03131", linestyle="")]
    fig.legend(handles, ["реакция на игру", "разговорный слой", "донат", "повод озвучен", "повод потерян"],
               loc="upper right", ncol=5, frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(CHARTS / "timeline.png", dpi=150)
    plt.close(fig)


def main():
    RESULTS.mkdir(exist_ok=True)
    CHARTS.mkdir(exist_ok=True)
    results = simulate_all()
    (RESULTS / "summary.md").write_text(summary_markdown(results), encoding="utf-8", newline="\n")

    # Журнал решений одного прогона — тем же отчётом, что читается после живого эфира
    samples = []
    for cls in (NaiveGates, DirectorV1, Director):
        policy = cls(dota_reportage(1))
        finalize(policy, run(policy))
        samples.append(policy)
        if cls is NaiveGates:
            continue
        stem = "director_v1" if cls is DirectorV1 else "director"
        with open(RESULTS / f"journal_{stem}_seed1.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for rec in policy.journal.records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        (RESULTS / f"report_{stem}_seed1.txt").write_text(
            render(summarize(policy.journal.records)) + "\n", encoding="utf-8", newline="\n")
    draw(results, samples)
    print(summary_markdown(results))


if __name__ == "__main__":
    main()
