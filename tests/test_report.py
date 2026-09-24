from director.report import advice, percentile, summarize


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert round(percentile(list(range(1, 11)), 0.9), 1) == 9.1


def test_tight_ttl_and_budget_advice():
    records = [{"t": 0, "e": "start"}]
    for i in range(10):
        records.append({"t": i, "e": "grant", "src": "caster", "score": 6})
        records.append({"t": i, "e": "commit", "src": "caster", "gen": 7.5 + i * 0.1, "ttl": 9})
        records.append({"t": i + 5, "e": "said", "src": "caster", "chars": 100})
    records += [{"t": 20, "e": "deny", "src": "caster", "why": "бюджет речи"}] * 10
    layer = summarize(records)["layers"]["caster"]
    assert layer["asked"] == 20 and layer["said"] == 10
    assert layer["tight"], "p90 задержки 8.3 при сроке 9 — реплики протухают до звука"
    tips = advice(summarize(records))
    assert any("протухают" in t for t in tips) and any("резерв" in t for t in tips)
