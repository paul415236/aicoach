"""跑步分類與訓練框架分析（純計算，無 Flask / 外部服務依賴）。

本模組集中 AI 分析與訓練統計共用的加權分類演算法：
- classify_run：單筆跑步的加權分類
- build_framework：由歷史跑步統計訓練框架
- estimate_hrmax / pace_anchors / hr_zones_by_pct：配速與心率推估
- detect_interval_progression：間歇課單趟距離趨勢分析
以及配速字串/秒數互轉等小工具。
"""
import datetime
import re as _re_mod


def _pace_str_to_sec(p):
    """'4:35' -> 275 秒/公里；無法解析回 None。"""
    try:
        m, s = str(p).split(":")[:2]
        return int(m) * 60 + int(s)
    except Exception:
        return None


def _sec_to_pace_str(sec):
    if sec is None:
        return "--"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def estimate_hrmax(all_runs):
    """從歷史數據推估 HRmax：取觀測 max_hr 的高位穩健值。
    用前 3 高的最大值（等同觀測峰值），但要求該峰值不孤立（與第 3 高差距 <=8bpm）才採用，
    否則退回前 3 高的中位數，避免單筆雜訊。回傳 int 或 None。"""
    hrs = sorted((r.get("max_hr") for r in all_runs if r.get("max_hr")), reverse=True)
    if not hrs:
        return None
    top = hrs[:3]
    peak = top[0]
    if len(top) >= 3 and (top[0] - top[2]) <= 8:
        return int(peak)          # 峰值穩定，直接採用
    return int(top[len(top) // 2])  # 否則取中位數抗雜訊


def _pace_zone_by_mp(pace_sec, mp_sec):
    """依配速相對 MP 的差距，回傳 E/M/T/I 之一（供加權分類的配速維度）。"""
    if pace_sec is None or mp_sec is None:
        return None
    diff = pace_sec - mp_sec  # 正=比MP慢
    if diff >= 45:
        return "E"
    if diff >= -5:
        return "M"          # MP 附近
    if diff >= -20:
        return "T"
    return "I"             # 比 MP 快超過 20 秒


def _hr_zone(pct):
    """依 %HRmax 回傳區間標籤。"""
    if pct is None:
        return None
    if pct < 0.80:
        return "E"
    if pct < 0.88:
        return "M"
    if pct < 0.93:
        return "T"
    return "I"


def classify_run(r, mp_sec, hrmax):
    """加權分類單筆跑步：心率0.40 + 距離0.35 + 配速0.25。
    回傳 (category, detail_dict)，category:
      'easy'          有氧輕鬆跑
      'aerobic_long'  有氧長跑（>=18km 且強度低）
      'quality_long'  質量長跑（>=18km 但心率>=80% 或配速接近/快於 MP）
      'quality'       間歇/節奏等質量課
      'other'         其他
    """
    name = r.get("name") or ""
    dist = r.get("distance_km") or 0
    pace_sec = _pace_str_to_sec(r.get("avg_pace"))
    avg_hr = r.get("avg_hr")
    pct = (avg_hr / hrmax) if (avg_hr and hrmax) else None

    hz = _hr_zone(pct)          # E/M/T/I（心率維度）
    pz = _pace_zone_by_mp(pace_sec, mp_sec)  # E/M/T/I（配速維度）

    # 各維度對「輕鬆有氧（easy/long）」的傾向分數 0~1
    hr_score = 1.0 if hz == "E" else (0.5 if hz == "M" else 0.0)
    if dist >= 18:
        dist_score = 1.0        # 長距離強烈傾向 long/easy
    elif dist >= 8:
        dist_score = 0.6
    else:
        dist_score = 0.15       # 短距離傾向間歇/質量
    pace_score = 1.0 if pz == "E" else (0.5 if pz == "M" else 0.0)

    easy_weight = 0.40 * hr_score + 0.35 * dist_score + 0.25 * pace_score

    # 明確的間歇/tempo 結構（名稱）→ 直接標記 quality（但長距離的 tempo 仍可能是質量長跑）
    has_interval_name = any(x in name for x in ["×", "x", "m@", "Tempo", "tempo",
                                                "000m", "600m", "800m", "400m", "LT", "間歇"])

    detail = {"dist": dist, "pace_sec": pace_sec, "avg_hr": avg_hr,
              "pct": pct, "hr_zone": hz, "pace_zone": pz, "easy_weight": easy_weight}

    # 長距離（>=18km）：細分「有氧長跑」與「質量長跑」
    # 門檻：心率 >=80% HRmax，或(78~80%灰色帶且配速接近/快於 MP) → 質量長跑
    if dist >= 18 and not has_interval_name:
        is_quality_long = False
        if pct is not None:
            if pct >= 0.80:
                is_quality_long = True
            elif pct >= 0.78 and pace_sec and mp_sec and pace_sec <= mp_sec + 30:
                is_quality_long = True
        elif pace_sec and mp_sec and pace_sec <= mp_sec + 20:
            # 無心率時退回配速判斷
            is_quality_long = True
        return ("quality_long" if is_quality_long else "aerobic_long"), detail

    # 分類邏輯（非長距離）：
    if easy_weight >= 0.55:
        return "easy", detail
    if has_interval_name or (pct and pct >= 0.88) or (pz in ("T", "I")):
        return "quality", detail
    return "other", detail


def build_framework(runs, mp_sec, hrmax):
    """統計跑者訓練框架。回傳文字摘要 + 結構化 dict。"""
    import statistics
    buckets = {"easy": [], "aerobic_long": [], "quality_long": [],
               "quality": [], "other": []}
    for r in runs:
        cat, d = classify_run(r, mp_sec, hrmax)
        buckets[cat].append((r, d))

    def med(xs):
        return statistics.median(xs) if xs else None

    # C：easy/long 的「配速錨點」只取有氧課，避免被快長跑/質量課污染
    easy_d = [d["dist"] for _, d in buckets["easy"] if d["dist"] and d["dist"] >= 8]
    easy_p = [d["pace_sec"] for _, d in buckets["easy"] if d["pace_sec"] and d["dist"] and d["dist"] >= 8]
    # long 統計「所有長跑」的距離（含質量長跑，代表慣用長跑距離），
    # 但 long 配速錨點只取「有氧長跑」（quality_long 不參與，避免污染）
    all_long = buckets["aerobic_long"] + buckets["quality_long"]
    long_d = [d["dist"] for _, d in all_long if d["dist"]]
    long_p = [d["pace_sec"] for _, d in buckets["aerobic_long"] if d["pace_sec"]]

    # 週里程（以 ISO 週聚合後取中位數，較能代表常態週）
    from collections import defaultdict
    wk = defaultdict(float)
    for r in runs:
        try:
            dt = datetime.date.fromisoformat(r["date"])
            wk[dt.isocalendar()[:2]] += r.get("distance_km") or 0
        except Exception:
            pass
    week_vals = sorted(wk.values(), reverse=True)
    # 去掉可能不完整的頭尾週後取中位數
    core_weeks = week_vals[1:-1] if len(week_vals) >= 4 else week_vals
    week_km_med = med(core_weeks) if core_weeks else (med(week_vals) if week_vals else 0)

    n_weeks = max(len(wk), 1)
    # D：質量負荷 = 間歇/節奏質量課 + 質量長跑（快長跑計入）
    n_quality_total = len(buckets["quality"]) + len(buckets["quality_long"])
    quality_per_week = n_quality_total / n_weeks

    # 將 quality bucket 再細分為「節奏跑(tempo)」與「間歇(interval)」
    def is_tempo(r):
        n = r.get("name") or ""
        return any(x in n for x in ["Tempo", "tempo", "LT", "節奏"])
    tempo_items = [(r, d) for (r, d) in buckets["quality"] if is_tempo(r)]
    interval_items = [(r, d) for (r, d) in buckets["quality"] if not is_tempo(r)]

    # 節奏跑慣用距離（近期）：取節奏跑的距離中位與最大
    tempo_d = [d["dist"] for _, d in tempo_items if d["dist"]]
    tempo_dist_med = med(tempo_d)
    tempo_dist_max = max(tempo_d) if tempo_d else None

    # 各區實際心率範圍（用分類結果回推跑者個人的「分區」心率，供遞增且一致）
    def hr_of(items):
        hrs = [d["avg_hr"] for _, d in items if d["avg_hr"]]
        return (int(min(hrs)), int(max(hrs))) if hrs else None
    easy_hr = hr_of(buckets["easy"])
    aerobic_long_hr = hr_of(buckets["aerobic_long"])
    tempo_hr = hr_of(tempo_items)
    interval_hr = hr_of(interval_items)
    quality_long_hr = hr_of(buckets["quality_long"])
    # 合併的有氧心率（easy+有氧長跑）供整體參考
    easy_all_hr = hr_of(buckets["easy"] + buckets["aerobic_long"])

    fw = {
        "hrmax": hrmax,
        "easy_dist_med": med(easy_d), "easy_dist_range": (min(easy_d), max(easy_d)) if easy_d else None,
        "long_dist_med": med(long_d), "long_dist_range": (min(long_d), max(long_d)) if long_d else None,
        "easy_pace_range": (min(easy_p), max(easy_p)) if easy_p else None,
        "long_pace_range": (min(long_p), max(long_p)) if long_p else None,
        "tempo_dist_med": tempo_dist_med, "tempo_dist_max": tempo_dist_max,
        "week_km_med": week_km_med,
        "quality_per_week": quality_per_week,
        "n_easy": len(buckets["easy"]),
        "n_aerobic_long": len(buckets["aerobic_long"]),
        "n_quality_long": len(buckets["quality_long"]),
        "n_quality": len(buckets["quality"]),
        # 分區心率（供每區帶自己的心率，遞增且兩處一致）
        "easy_hr": easy_hr,
        "aerobic_long_hr": aerobic_long_hr,
        "tempo_hr": tempo_hr,
        "interval_hr": interval_hr,
        "quality_long_hr": quality_long_hr,
        "easy_all_hr": easy_all_hr,
    }
    return fw, buckets


def pace_anchors(mp_sec):
    """由目標 MP（秒/km）推導各區參考配速（經驗係數，回傳範圍秒）。
    間歇依距離分：長間歇(>=1600m)≈VO2max 較慢；短間歇(<1600m) 更快。"""
    if not mp_sec:
        return None
    return {
        "E": (mp_sec + 50, mp_sec + 80),        # 輕鬆跑: MP+0:50~1:20
        "M": (mp_sec, mp_sec),                  # 馬拉松配速跑 = MP
        "T": (mp_sec - 12, mp_sec - 8),         # 節奏跑: MP-0:08~0:12
        "long_interval": (mp_sec - 25, mp_sec - 18),   # 長間歇(>=1600m): MP-0:18~0:25
        "short_interval": (mp_sec - 38, mp_sec - 30),  # 短間歇(<1600m): MP-0:30~0:38
    }


def hr_zones_by_pct(hrmax, easy_hr_measured=None):
    """以 %HRmax 理論區間為主幹回傳各區心率(bpm, 遞增、寬度合理)。
    easy_hr_measured: (min,max) 實測輕鬆跑心率，若提供則用來校準 easy 邊界。
    區間(%HRmax): Easy 65~78 / Long 70~80 / M 80~88 / T 88~92 / 長間歇 92~96 / 短間歇 95~100"""
    if not hrmax:
        return None
    def bpm(lo, hi):
        return (int(round(hrmax * lo)), int(round(hrmax * hi)))
    zones = {
        "easy": bpm(0.65, 0.78),
        "long": bpm(0.70, 0.80),
        "M": bpm(0.80, 0.88),
        "T": bpm(0.88, 0.92),
        "long_interval": bpm(0.92, 0.96),
        "short_interval": bpm(0.95, 1.00),
    }
    # 用實測輕鬆跑心率校準 easy 邊界（取理論與實測的合理聯集，不超過 M 下緣）
    if easy_hr_measured:
        elo, ehi = easy_hr_measured
        tlo, thi = zones["easy"]
        zones["easy"] = (min(tlo, elo), min(max(thi, ehi), zones["M"][0] - 1))
    return zones


def detect_interval_progression(runs):
    """依時間序列分析間歇課的單趟距離趨勢，判定當前階段與推進方向。
    回傳 dict 或 None（資料不足）。
    - stage: 'short'|'long'  當前所在階段
    - trend: 'progressing'|'stable'|'regressing'  近段相對前段的單趟距離趨勢
    - recent_reps: 最近幾次間歇課的單趟距離(m) 列表(時間由舊到新)
    - summary: 中文摘要句
    """
    def rep_len(name):
        # 取名稱中最大的單趟距離（避免 400m 恢復段干擾主課），單位 m
        nums = _re_mod.findall(r'(\d{3,4})\s*m', name or '', _re_mod.I)
        return max((int(x) for x in nums), default=None)

    # 抽出「有明確反覆結構」的間歇課（排除純 Tempo）
    items = []
    for r in sorted(runs, key=lambda x: x.get("date", "")):
        name = r.get("name") or ""
        if not any(x in name for x in ["×", "x", "m@", "400m", "600m", "800m",
                                       "000m", "600m", "間歇"]):
            continue
        rl = rep_len(name)
        if rl:
            items.append((r.get("date", ""), rl))

    if len(items) < 2:
        return None

    reps = [rl for _, rl in items]
    recent = reps[-3:]
    # 當前階段：最近 2 次間歇的單趟距離中位偏大即視為長間歇階段
    last2 = reps[-2:]
    stage = "long" if (sum(1 for x in last2 if x >= 1600) >= 1 and last2[-1] >= 1600) else "short"

    # 趨勢：比較前半段與後半段的平均單趟距離
    half = max(len(reps) // 2, 1)
    early_avg = sum(reps[:half]) / half
    late_avg = sum(reps[half:]) / (len(reps) - half)
    if late_avg > early_avg * 1.15:
        trend = "progressing"
    elif late_avg < early_avg * 0.85:
        trend = "regressing"
    else:
        trend = "stable"

    stage_zh = "長間歇" if stage == "long" else "短間歇"
    trend_zh = {"progressing": "單趟距離持續拉長（正往長間歇推進）",
                "stable": "單趟距離大致穩定",
                "regressing": "單趟距離縮短"}[trend]
    recent_max = max(recent)  # 近期已達到的最大單趟距離
    summary = (f"間歇課單趟距離依時間為 {reps} m；最近為 {recent} m。"
               f"目前處於「{stage_zh}」階段，趨勢：{trend_zh}。近期最大單趟 {recent_max}m。")
    stage_en = "long intervals" if stage == "long" else "short intervals"
    trend_en = {"progressing": "rep distance steadily increasing (advancing to long intervals)",
                "stable": "rep distance roughly stable",
                "regressing": "rep distance shortening"}[trend]
    summary_en = (f"Interval rep distances over time: {reps} m; recent: {recent} m. "
                  f"Currently in the \"{stage_en}\" phase; trend: {trend_en}. "
                  f"Recent max rep {recent_max}m.")
    return {"stage": stage, "trend": trend, "recent_reps": recent,
            "recent_max": recent_max, "all_reps": reps,
            "summary": summary, "summary_en": summary_en}
