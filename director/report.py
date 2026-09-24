"""Отчёт по журналу решений: что бот сказал, почему молчал и какие пороги мешают.

    python -m director.report logs/director_2026-09-12.jsonl

Главное число — задержка выдача→звук (p90) против срока годности слоя: если p90
подползает к сроку, реплики протухают ещё до первого слова, и настройка там ТЕСНАЯ.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

from .journal import read

TIGHT_SHARE = 0.8     # p90 задержки выше 80% срока годности — тесно


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def summarize(records: list[dict]) -> dict:
    asked, granted, said = Counter(), Counter(), Counter()
    reasons: dict[str, Counter] = defaultdict(Counter)
    cancels: dict[str, Counter] = defaultdict(Counter)
    delays: dict[str, list[float]] = defaultdict(list)
    ttl: dict[str, float] = {}
    said_chars: dict[str, list[int]] = defaultdict(list)
    said_times: list[float] = []

    for r in records:
        src = r.get("src")
        if r["e"] == "grant":
            asked[src] += 1
            granted[src] += 1
        elif r["e"] == "deny":
            asked[src] += 1
            reasons[src][r["why"]] += 1
        elif r["e"] == "commit":
            delays[src].append(r["gen"])
            ttl[src] = r.get("ttl", ttl.get(src, 0.0))
        elif r["e"] == "cancel":
            cancels[src][r["why"]] += 1
        elif r["e"] == "said":
            said[src] += 1
            said_chars[src].append(r.get("chars", 0))
            said_times.append(r["t"])

    layers = {}
    for src in sorted(set(asked) | set(said)):
        d = delays.get(src, [])
        p90 = percentile(d, 0.9)
        layers[src] = {
            "asked": asked[src], "granted": granted[src], "said": said[src],
            "deny_reasons": dict(reasons[src].most_common()),
            "cancel_reasons": dict(cancels[src].most_common()),
            "delay_p50": round(percentile(d, 0.5), 2), "delay_p90": round(p90, 2),
            "ttl": ttl.get(src), "tight": bool(ttl.get(src) and p90 > TIGHT_SHARE * ttl[src]),
            "chars_median": int(percentile(said_chars[src], 0.5)) if said_chars[src] else 0,
        }
    gaps = sorted((b - a for a, b in zip(said_times, said_times[1:])), reverse=True)
    return {"layers": layers, "longest_silences": [round(g, 1) for g in gaps[:5]]}


def advice(summary: dict) -> list[str]:
    """Конкретные правки, посчитанные по тем же цифрам, а не общие слова."""
    tips = []
    for src, layer in summary["layers"].items():
        if layer["tight"]:
            tips.append(f"{src}: p90 задержки {layer['delay_p90']}с при сроке годности {layer['ttl']}с — "
                        f"реплики протухают до звука; ускорить модель или поднять срок")
        budget = layer["deny_reasons"].get("бюджет речи", 0)
        if layer["asked"] and budget / layer["asked"] > 0.3:
            tips.append(f"{src}: {budget} из {layer['asked']} отказов по бюджету — проверить, "
                        f"совпадает ли резерв с реальной длиной реплик")
        owner = layer["deny_reasons"].get("стример комментирует сам", 0)
        if layer["asked"] and owner / layer["asked"] > 0.3:
            tips.append(f"{src}: {owner} из {layer['asked']} отказов «стример комментирует сам» — "
                        f"при сплошном репортаже совпадение речи с событием случайно")
    return tips


def render(summary: dict) -> str:
    lines = ["слой        просил  выдано  прозвучало  задержка p50/p90   срок"]
    for src, l in summary["layers"].items():
        flag = "  <-- ТЕСНО" if l["tight"] else ""
        lines.append(f"{src:<11} {l['asked']:>6}  {l['granted']:>6}  {l['said']:>10}  "
                     f"{l['delay_p50']:>6} / {l['delay_p90']:<6}  {l['ttl'] or '—'}{flag}")
    for src, l in summary["layers"].items():
        if l["deny_reasons"]:
            top = ", ".join(f"{why} — {n}" for why, n in list(l["deny_reasons"].items())[:4])
            lines.append(f"почему молчал {src}: {top}")
    lines.append(f"самые длинные паузы, с: {summary['longest_silences']}")
    tips = advice(summary)
    if tips:
        lines += ["", "что подкрутить:"] + [f"  • {t}" for t in tips]
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("использование: python -m director.report <журнал.jsonl>")
    print(render(summarize(read(sys.argv[1]))))
