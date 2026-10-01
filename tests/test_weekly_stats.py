"""weekly-stats 數字一致性測試。

兩層：
1. 不變量測試：對一組 runs 做分類聚合，驗證「各類加總 == 總量」「比例加總 == 100%」，
   不依賴 Flask / DB。
2. 整合測試：若存在真實 DB，實打 /api/weekly-stats，驗證週/月/全期間三者總量一致。
"""
import os

import classifier as C

HRMAX = 197
MP = 242

# 前端 5 類對應（與 server.weekly_stats 的 _map_cat 相同）
def _map_cat(run):
    cat, _ = C.classify_run(run, MP, HRMAX)
    if cat == "easy":
        return "easy"
    if cat in ("aerobic_long", "quality_long"):
        return "long"
    if cat == "quality":
        n = run.get("name") or ""
        is_tempo = any(x in n for x in ["Tempo", "tempo", "LT", "節奏"])
        return "tempo" if is_tempo else "interval"
    return "other"


SAMPLE_RUNS = [
    {"name": "輕鬆跑", "distance_km": 10, "duration_mins": 55, "avg_pace": "5:30", "avg_hr": 130, "date": "2026-01-05"},
    {"name": "長跑", "distance_km": 25, "duration_mins": 140, "avg_pace": "5:35", "avg_hr": 140, "date": "2026-01-07"},
    {"name": "1000m×5 間歇", "distance_km": 10, "duration_mins": 45, "avg_pace": "3:50", "avg_hr": 178, "date": "2026-01-09"},
    {"name": "Tempo 節奏跑", "distance_km": 12, "duration_mins": 50, "avg_pace": "4:10", "avg_hr": 170, "date": "2026-01-11"},
    {"name": "輕鬆跑", "distance_km": 8, "duration_mins": 44, "avg_pace": "5:40", "avg_hr": 128, "date": "2026-01-12"},
]

CATS = ["easy", "tempo", "interval", "long", "other"]


def test_category_sum_equals_total():
    buckets = {c: {"km": 0.0, "mins": 0.0} for c in CATS}
    for r in SAMPLE_RUNS:
        c = _map_cat(r)
        buckets[c]["km"] += r["distance_km"]
        buckets[c]["mins"] += r["duration_mins"]
    total_km = sum(r["distance_km"] for r in SAMPLE_RUNS)
    total_mins = sum(r["duration_mins"] for r in SAMPLE_RUNS)
    assert abs(sum(buckets[c]["km"] for c in CATS) - total_km) < 1e-6
    assert abs(sum(buckets[c]["mins"] for c in CATS) - total_mins) < 1e-6


def test_percentages_sum_to_100():
    buckets = {c: 0.0 for c in CATS}
    for r in SAMPLE_RUNS:
        buckets[_map_cat(r)] += r["distance_km"]
    total = sum(buckets.values())
    pcts = [round(buckets[c] / total * 100, 1) for c in CATS]
    # 容許四捨五入誤差 ±0.1
    assert abs(sum(pcts) - 100.0) <= 0.1


# ── 整合測試：真實 DB 存在時實打 API ──────────────────────
_DB = os.path.join(os.path.dirname(__file__), "..", "data", "garmin_running_history.db")


def test_weekly_stats_api_consistency():
    import pytest
    if not os.path.exists(_DB):
        pytest.skip("無真實 DB，略過整合測試")
    import server
    c = server.app.test_client()
    r = c.get("/api/weekly-stats")
    assert r.status_code == 200
    d = r.get_json()
    overall = d["overall"]["total_km"]
    wsum = round(sum(w["total_km"] for w in d["weeks"]), 1)
    msum = round(sum(m["total_km"] for m in d["months"]), 1)
    # 週加總 == 月加總 == 全期間（容許四捨五入誤差）
    assert abs(wsum - overall) < 1.0
    assert abs(msum - overall) < 1.0
    # 每個期間：各類 km 加總 == 該期間 total_km
    for w in d["weeks"]:
        s = round(sum(w["cats"][cat]["km"] for cat in d["categories"]), 2)
        assert abs(s - w["total_km"]) < 0.05
