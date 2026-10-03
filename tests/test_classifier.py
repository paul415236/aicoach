"""classifier.py 單元測試：分類演算法與配速/心率推估。"""
import classifier as C


# ── 配速字串/秒數互轉 ──────────────────────────────────────
def test_pace_str_to_sec():
    assert C._pace_str_to_sec("4:35") == 275
    assert C._pace_str_to_sec("5:00") == 300
    assert C._pace_str_to_sec("N/A") is None
    assert C._pace_str_to_sec(None) is None


def test_sec_to_pace_str():
    assert C._sec_to_pace_str(275) == "4:35"
    assert C._sec_to_pace_str(300) == "5:00"
    assert C._sec_to_pace_str(None) == "--"
    # 四捨五入
    assert C._sec_to_pace_str(274.6) == "4:35"


def test_pace_sec_roundtrip():
    for s in (180, 242, 275, 360):
        assert C._pace_str_to_sec(C._sec_to_pace_str(s)) == s


# ── estimate_hrmax ────────────────────────────────────────
def test_estimate_hrmax_empty():
    assert C.estimate_hrmax([]) is None
    assert C.estimate_hrmax([{"max_hr": None}]) is None


def test_estimate_hrmax_stable_peak():
    # 前三高差距 <=8，採峰值
    runs = [{"max_hr": v} for v in (195, 194, 192, 150, 140)]
    assert C.estimate_hrmax(runs) == 195


def test_estimate_hrmax_noisy_peak():
    # 單筆雜訊高值（210），與第三高差距 >8，退回前三高中位數
    runs = [{"max_hr": v} for v in (210, 190, 188, 150)]
    # top3 = [210,190,188], 210-188=22 >8 → 取中位數 190
    assert C.estimate_hrmax(runs) == 190


# ── _hr_zone / _pace_zone_by_mp ───────────────────────────
def test_hr_zone_boundaries():
    assert C._hr_zone(None) is None
    assert C._hr_zone(0.70) == "E"
    assert C._hr_zone(0.85) == "M"
    assert C._hr_zone(0.90) == "T"
    assert C._hr_zone(0.95) == "I"


def test_pace_zone_by_mp():
    mp = 242
    assert C._pace_zone_by_mp(mp + 60, mp) == "E"   # 慢於 MP 很多
    assert C._pace_zone_by_mp(mp, mp) == "M"        # MP 附近
    assert C._pace_zone_by_mp(mp - 15, mp) == "T"   # 比 MP 快一點
    assert C._pace_zone_by_mp(mp - 30, mp) == "I"   # 比 MP 快很多
    assert C._pace_zone_by_mp(None, mp) is None


# ── classify_run ──────────────────────────────────────────
HRMAX = 197
MP = 242


def _run(name="跑步", dist=10, pace="5:30", avg_hr=130):
    return {"name": name, "distance_km": dist, "avg_pace": pace, "avg_hr": avg_hr}


def test_classify_easy():
    # 低心率、中距離、慢配速 → easy
    cat, _ = C.classify_run(_run(dist=12, pace="5:40", avg_hr=130), MP, HRMAX)
    assert cat == "easy"


def test_classify_aerobic_long():
    # >=18km 且低強度 → aerobic_long
    cat, _ = C.classify_run(_run(dist=25, pace="5:30", avg_hr=135), MP, HRMAX)
    assert cat == "aerobic_long"


def test_classify_quality_long():
    # >=18km 但高心率 (>=80% HRmax) → quality_long
    cat, _ = C.classify_run(_run(dist=22, pace="4:15", avg_hr=170), MP, HRMAX)
    assert cat == "quality_long"


def test_classify_quality_by_name():
    # 名稱含間歇結構 → quality
    cat, _ = C.classify_run(_run(name="1000m×5 間歇", dist=10, pace="3:50", avg_hr=175), MP, HRMAX)
    assert cat == "quality"


def test_classify_quality_by_pace():
    # 短距離、快配速（T/I 區）、高心率 → quality
    cat, _ = C.classify_run(_run(dist=6, pace="4:00", avg_hr=180), MP, HRMAX)
    assert cat == "quality"


# ── pace_anchors ──────────────────────────────────────────
def test_pace_anchors_ordering():
    a = C.pace_anchors(MP)
    assert a is not None
    # 由慢到快：E > M > T > long_interval > short_interval（秒數遞減）
    assert a["E"][0] > a["M"][0] >= a["T"][0] > a["long_interval"][0] > a["short_interval"][0]
    assert a["M"] == (MP, MP)


def test_pace_anchors_none():
    assert C.pace_anchors(None) is None
    assert C.pace_anchors(0) is None


# ── hr_zones_by_pct ───────────────────────────────────────
def test_hr_zones_increasing():
    z = C.hr_zones_by_pct(HRMAX)
    assert z is not None
    # 各區下緣由慢到快遞增
    order = ["easy", "long", "M", "T", "long_interval", "short_interval"]
    los = [z[k][0] for k in order]
    assert los == sorted(los)


def test_hr_zones_none():
    assert C.hr_zones_by_pct(None) is None


# ── detect_interval_progression ───────────────────────────
def test_interval_progression_insufficient():
    assert C.detect_interval_progression([{"name": "輕鬆跑", "date": "2026-01-01"}]) is None


def test_interval_progression_advancing():
    runs = [
        {"name": "800m×6 間歇", "date": "2026-01-01"},
        {"name": "1000m×5 間歇", "date": "2026-01-08"},
        {"name": "1600m×4 間歇", "date": "2026-01-15"},
        {"name": "2000m×3 間歇", "date": "2026-01-22"},
    ]
    res = C.detect_interval_progression(runs)
    assert res is not None
    assert res["stage"] == "long"          # 最近達到 2000m
    assert res["trend"] == "progressing"   # 單趟距離持續拉長
    assert res["recent_max"] == 2000


# ── is_tempo_run / map_category / monthly_type_summary ────
def test_is_tempo_run():
    assert C.is_tempo_run({"name": "Tempo 10k"}) is True
    assert C.is_tempo_run({"name": "節奏跑"}) is True
    assert C.is_tempo_run({"name": "1000m×5 間歇"}) is False
    assert C.is_tempo_run({"name": "輕鬆跑"}) is False


def test_map_category():
    assert C.map_category(_run(dist=12, pace="5:40", avg_hr=130), MP, HRMAX) == "easy"
    assert C.map_category(_run(dist=25, pace="5:30", avg_hr=135), MP, HRMAX) == "long"
    assert C.map_category(_run(name="節奏跑", dist=10, pace="4:05", avg_hr=175), MP, HRMAX) == "tempo"
    assert C.map_category(_run(name="1000m×5", dist=10, pace="3:50", avg_hr=180), MP, HRMAX) == "interval"


def test_monthly_type_summary_empty():
    summ = C.monthly_type_summary([], MP, HRMAX)
    assert set(summ.keys()) == {"easy", "tempo", "interval", "long"}
    for v in summ.values():
        assert v["count"] == 0 and v["total_km"] == 0.0
        assert v["avg_pace"] is None and v["avg_hr"] is None


def test_monthly_type_summary_values():
    runs = [
        _run(name="輕鬆跑", dist=10, pace="5:30", avg_hr=130),
        _run(name="輕鬆跑", dist=12, pace="5:40", avg_hr=134),
        _run(name="節奏跑", dist=10, pace="4:05", avg_hr=175),
    ]
    # 補 max_hr
    for r in runs:
        r["max_hr"] = r["avg_hr"] + 15
    summ = C.monthly_type_summary(runs, MP, HRMAX)
    assert summ["easy"]["count"] == 2
    assert summ["easy"]["total_km"] == 22.0
    assert summ["easy"]["avg_pace"] == "5:35"   # (330+340)/2=335 → 5:35
    assert summ["tempo"]["count"] == 1
    assert summ["tempo"]["max_hr"] == 190


# ── VDOT / 等效成績 / 效率指標 ─────────────────────────────
def test_vdot_matches_daniels_table():
    # Daniels VDOT 表：5K 19:57 與 10K 41:21 皆對應 VDOT≈50
    assert abs(C.vdot_from_run(5.0, 19 + 57/60) - 50.0) < 0.3
    assert abs(C.vdot_from_run(10.0, 41 + 21/60) - 50.0) < 0.3


def test_vdot_faster_is_higher():
    # 同距離更快 → VDOT 更高（直覺一致性，正是修掉 AI 心算矛盾的重點）
    faster = C.vdot_from_run(15.34, 57.9)   # 3:46/km
    slower = C.vdot_from_run(15.24, 59.0)   # 3:52/km
    assert faster > slower


def test_vdot_invalid():
    assert C.vdot_from_run(0, 30) is None
    assert C.vdot_from_run(10, 0) is None
    assert C.vdot_from_run(None, None) is None


def test_equivalent_races():
    eq = C.equivalent_races(50)
    # 對照 Daniels 表（容許 ±2 秒的數值求解誤差）
    assert eq["5K"].startswith("19:5")
    assert eq["10K"].startswith("41:")
    assert eq["Half"].startswith("1:31")
    assert eq["Full"].startswith("3:10")
    assert C.equivalent_races(None) is None


def test_hr_drift_pct():
    splits = [{"avg_hr": h} for h in [150, 155, 160, 165, 170, 175, 180, 182]]
    drift, early, late = C.hr_drift_pct(splits)
    # 前 2 圈平均 152.5、後 2 圈平均 181 → 約 +18.7%
    assert drift > 0 and late > early
    assert C.hr_drift_pct([{"avg_hr": 150}])[0] is None   # 不足 4 圈


def test_intensity_pct():
    assert C.intensity_pct(174, 197) == 88
    assert C.intensity_pct(None, 197) is None


def test_pace_cv_pct():
    # 配速很穩 → CV 小
    stable = [{"avg_pace": "4:00"}, {"avg_pace": "4:01"}, {"avg_pace": "3:59"}]
    assert C.pace_cv_pct(stable) < 1.0
    assert C.pace_cv_pct([{"avg_pace": "4:00"}]) is None   # 不足 2 圈
