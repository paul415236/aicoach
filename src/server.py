#!/usr/bin/env python3
import json
import os
import queue
import sqlite3
import threading
import datetime
from flask import Flask, Response, send_from_directory

import sys

app = Flask(__name__)

# src/ 執行時，data/ 在上一層；PyInstaller exe 執行時 data 與 exe 同目錄
_HERE = os.path.dirname(os.path.abspath(__file__))
_IS_FROZEN = hasattr(sys, '_MEIPASS')
BASE_DIR = sys._MEIPASS if _IS_FROZEN else _HERE
# frozen: exe 所在目錄（sys.executable 的目錄）；開發: ../data
DATA_DIR = os.path.dirname(sys.executable) if _IS_FROZEN else os.path.join(_HERE, '..', 'data')

DB_FILE = os.path.join(DATA_DIR, "garmin_running_history.db")
AI_PLAN_FILE = os.path.join(DATA_DIR, "ai_plan.json")

# ── MFA coordination ──────────────────────────────────────
# When a sync job needs an MFA code, it registers a queue here keyed by a
# session id and blocks until the browser POSTs the code to /api/mfa.
_MFA_LOCK = threading.Lock()
_MFA_PENDING = {}  # session_id -> queue.Queue (receives the submitted code)


def _register_mfa_waiter(session_id):
    q = queue.Queue(maxsize=1)
    with _MFA_LOCK:
        _MFA_PENDING[session_id] = q
    return q


def _resolve_mfa(session_id, code):
    with _MFA_LOCK:
        q = _MFA_PENDING.pop(session_id, None)
    if q is None:
        return False
    q.put(code)
    return True


def _clear_mfa_waiter(session_id):
    with _MFA_LOCK:
        _MFA_PENDING.pop(session_id, None)

def query(sql, args=()):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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

    # 各區實際心率範圍（用分類結果回推跑者個人的區間心率）
    def hr_range(cats):
        hrs = [d["avg_hr"] for c in cats for _, d in buckets[c] if d["avg_hr"]]
        return (int(min(hrs)), int(max(hrs))) if hrs else None
    # easy/long 心率只取有氧課（不含質量長跑）
    easy_hr = hr_range(["easy", "aerobic_long"])
    quality_hr = hr_range(["quality", "quality_long"])

    fw = {
        "hrmax": hrmax,
        "easy_dist_med": med(easy_d), "easy_dist_range": (min(easy_d), max(easy_d)) if easy_d else None,
        "long_dist_med": med(long_d), "long_dist_range": (min(long_d), max(long_d)) if long_d else None,
        "easy_pace_range": (min(easy_p), max(easy_p)) if easy_p else None,
        "long_pace_range": (min(long_p), max(long_p)) if long_p else None,
        "week_km_med": week_km_med,
        "quality_per_week": quality_per_week,
        "n_easy": len(buckets["easy"]),
        "n_aerobic_long": len(buckets["aerobic_long"]),
        "n_quality_long": len(buckets["quality_long"]),
        "n_quality": len(buckets["quality"]),
        "easy_hr": easy_hr, "quality_hr": quality_hr,
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


TERMINOLOGY_ZH = """【術語規範（全篇「務必」統一使用下列「六種」標準中文名稱，禁止使用其他同義詞或自創詞；課表類型只能是這六種之一）】
1. 「輕鬆跑」= Easy = E。嚴禁使用「易感跑／易感／易感帶／恢復跑／輕跑」等說法。
2. 「馬拉松配速跑」= Marathon Pace = M。
3. 「節奏跑」= Tempo = Threshold = T。嚴禁把「閾值跑／乳酸跑」當作課表名稱（可在說明中提及乳酸閾值原理，但課表類型一律稱「節奏跑」）。
4. 「短間歇」= 單趟距離「小於 1600m」的間歇（如 400m/600m/800m/1000m 反覆），配速最快。
5. 「長間歇」= 單趟距離「1600m 以上」的間歇（如 1600m/2000m 反覆），配速略慢於短間歇。
6. 「長跑」= Long Run = LSD。
* 不要再使用「間歇」「反覆跑」「Interval」「Rep」等籠統或其他名稱；所有間歇課一律依單趟距離歸為「短間歇」或「長間歇」。
* 首次出現各類型時可用「中文（英文/代號）」標註一次，例如「輕鬆跑（Easy, E）」，之後一律用中文名稱；不要中英文混雜或多詞交替。"""

TERMINOLOGY_EN = """[Terminology rules — use ONLY these six zone names consistently; do NOT invent synonyms]
1. Easy (E)
2. Marathon Pace (M)
3. Tempo / Threshold (T)
4. Short Interval — reps shorter than 1600m (e.g. 400m/600m/800m/1000m), fastest pace
5. Long Interval — reps 1600m or longer (e.g. 1600m/2000m), slightly slower than short intervals
6. Long Run (LSD)
* Do not use generic "Interval"/"Rep"; classify every interval workout as Short Interval or Long Interval by rep distance.
* Pick one label per zone and use it consistently; do not alternate between synonyms."""


import re as _re_mod


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
    return {"stage": stage, "trend": trend, "recent_reps": recent,
            "recent_max": recent_max, "all_reps": reps, "summary": summary}


def normalize_terms(text):
    """對 AI 輸出做確定性術語正規化，兜底修正模型未遵守術語規範的情況。
    - 易感跑/易感帶/易感 → 輕鬆跑
    - 數值範圍的半形波浪號 ~ → 全形 ～（避免 marked.js 的 GFM 刪除線把 ~a~b~ 劃線）
    """
    if not text:
        return text
    # 先處理較長的詞，避免「易感跑」被先換成「輕鬆跑跑」
    text = text.replace("易感跑", "輕鬆跑")
    text = text.replace("易感帶", "輕鬆跑配速帶")
    text = text.replace("易感", "輕鬆")
    # 表示範圍的波浪號（被數字/字母/冒號/百分號夾住）轉全形，避免觸發刪除線
    import re as _re
    text = _re.sub(r'(?<=[0-9A-Za-z:%）)])\s*~\s*(?=[0-9A-Za-z:%（(])', '～', text)
    return text


def json_resp(data):
    return app.response_class(
        response=json.dumps(data, ensure_ascii=False),
        mimetype='application/json; charset=utf-8'
    )

def run_job(fn):
    """Run fn in a thread, stream log lines as SSE.

    fn is called as fn(log, emit_event):
      - log(msg): stream a normal log line (SSE `data:`)
      - emit_event(name, payload_dict): stream a named SSE control event
        (e.g. emit_event("mfa", {"session_id": ...}))
    """
    import traceback
    q = queue.Queue()

    def log(msg):
        q.put(("log", str(msg)))

    def emit_event(name, payload):
        q.put(("event", (name, payload)))

    def worker():
        try:
            fn(log, emit_event)
        except Exception:
            q.put(("log", "❌ " + traceback.format_exc()))
        finally:
            q.put(("done", None))
    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            kind, payload = q.get()
            if kind == "done":
                yield "event: done\ndata: \n\n"
                break
            elif kind == "event":
                name, data = payload
                yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            else:  # log
                yield f"data: {payload}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── routes ────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "dashboard.html")

@app.route("/api/runs")
def get_runs():
    return json_resp(query("SELECT * FROM runs ORDER BY date DESC"))

@app.route("/api/runs/<int:activity_id>/splits")
def get_splits(activity_id):
    return json_resp(query(
        "SELECT * FROM activity_splits WHERE activity_id=? ORDER BY lap",
        (activity_id,)
    ))

@app.route("/api/ai-plan")
def get_ai_plan():
    if not os.path.exists(AI_PLAN_FILE):
        return json_resp({"content": None, "generated_at": None})
    with open(AI_PLAN_FILE, encoding="utf-8") as f:
        return json_resp(json.load(f))

ANALYZE_CONFIG_FILE = os.path.join(DATA_DIR, "analyze_config.json")

@app.route("/api/analyze-config", methods=["GET"])
def get_analyze_config():
    if not os.path.exists(ANALYZE_CONFIG_FILE):
        return app.response_class(status=204)
    with open(ANALYZE_CONFIG_FILE, encoding="utf-8") as f:
        return json_resp(json.load(f))

@app.route("/api/analyze-config", methods=["POST"])
def save_analyze_config():
    from flask import request as freq
    data = freq.get_json(silent=True) or {}
    with open(ANALYZE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return json_resp({"ok": True})

@app.route("/api/mfa", methods=["POST"])
def submit_mfa():
    from flask import request as freq
    data = freq.get_json(silent=True) or {}
    session_id = data.get("session_id")
    code = (data.get("code") or "").strip()
    if not session_id or not code:
        return json_resp({"ok": False, "error": "missing session_id or code"})
    if _resolve_mfa(session_id, code):
        return json_resp({"ok": True})
    return json_resp({"ok": False, "error": "no pending MFA for this session"})

@app.route("/api/sync", methods=["POST"])
def sync():
    def job(log, emit_event):
        import uuid
        from dotenv import load_dotenv
        import garth
        from garminconnect import Garmin, GarminConnectAuthenticationError

        # frozen exe: .env 在 exe 同目錄；開發: 專案根目錄
        exe_dir = os.path.dirname(sys.executable) if _IS_FROZEN else os.path.join(_HERE, '..')
        env_path = os.path.join(exe_dir, '.env')
        load_dotenv(env_path)
        EMAIL = os.getenv("GARMIN_EMAIL")
        PASSWORD = os.getenv("GARMIN_PASSWORD")
        log(f"📁 DB: {DB_FILE}")
        log(f"📄 .env: {env_path} ({'found' if os.path.exists(env_path) else 'NOT FOUND'})")
        log(f"👤 Email: {EMAIL or 'NOT SET'}")

        if not EMAIL or not PASSWORD:
            log("❌ 請確認 .env 檔案放在 exe 同目錄，並設定 GARMIN_EMAIL 與 GARMIN_PASSWORD")
            return

        TOKEN_DIR = os.path.join(DATA_DIR, ".garminconnect_token")

        # ── Cloudflare bypass ──────────────────────────────────────
        # Since Garmin's March 2026 change, their Cloudflare layer returns
        # empty 200 [] bodies to garth's default mobile User-Agent
        # (GCM-*). Overriding it with a browser UA is enough to get real
        # data back. This must be applied to every garth session used.
        _BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36")

        def _patch_ua(garth_client):
            try:
                garth_client.sess.headers.update({"User-Agent": _BROWSER_UA})
            except Exception:
                pass

        # module-level client used by garth.login / garth.save
        _patch_ua(garth.client)

        def _make_api():
            """Construct a Garmin() with the browser UA applied to its client."""
            api = Garmin()
            _patch_ua(api.garth)
            return api

        def prompt_mfa():
            """Called by garth when Garmin requires an MFA code.
            Asks the browser for the code and blocks until it's submitted."""
            session_id = uuid.uuid4().hex
            wait_q = _register_mfa_waiter(session_id)
            log("🔐 需要 MFA 驗證碼，請在網頁輸入")
            emit_event("mfa", {"session_id": session_id})
            try:
                # Block up to 5 minutes for the user to submit the code.
                code = wait_q.get(timeout=300)
            except queue.Empty:
                _clear_mfa_waiter(session_id)
                raise GarminConnectAuthenticationError("MFA 驗證逾時（5 分鐘未輸入）")
            log("🔑 已收到 MFA 驗證碼，繼續登入...")
            return code

        def full_login():
            """Username/password login (handles MFA) then persist tokens."""
            garth.login(EMAIL, PASSWORD, prompt_mfa=prompt_mfa)
            os.makedirs(TOKEN_DIR, exist_ok=True)
            garth.save(TOKEN_DIR)
            api = _make_api()
            api.login(TOKEN_DIR)
            return api

        log("🔄 正在嘗試登入 Garmin Connect...")
        api = None
        if os.path.isdir(TOKEN_DIR):
            try:
                api = _make_api()
                api.login(TOKEN_DIR)
                log("✅ 使用快取 Token 登入成功！")
            except (GarminConnectAuthenticationError, AssertionError, KeyError,
                    ValueError, TypeError) as e:
                # Expired/invalid token surfaces in several ways depending on
                # garth version (AssertionError from an empty profile body,
                # KeyError on missing fields, etc). Treat all as "re-login".
                log(f"🔑 快取 Token 無效或已過期（{type(e).__name__}），改用帳號密碼重新登入...")
                api = None

        if api is None:
            api = full_login()
            log("✅ 帳密登入成功！")

        # inline sync logic (compatible with frozen exe)
        from datetime import datetime, timedelta

        def _parse_pace(speed_ms):
            if speed_ms and speed_ms > 0:
                p = 16.6667 / speed_ms
                return f"{int(p)}:{int((p - int(p)) * 60):02d}"
            return "N/A"

        conn = sqlite3.connect(DB_FILE, timeout=10)
        conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            activity_id INTEGER PRIMARY KEY, date TEXT, name TEXT,
            distance_km REAL, duration_mins REAL, elevation_gain_m REAL,
            avg_pace TEXT, avg_hr REAL, max_hr REAL, avg_cadence REAL,
            training_effect REAL, anaerobic_effect REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS activity_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id INTEGER,
            lap INTEGER, distance_km REAL, duration_mins REAL,
            avg_pace TEXT, avg_hr REAL, max_hr REAL, avg_cadence REAL,
            elevation_gain_m REAL)""")
        conn.commit()
        existing = {r[0] for r in conn.execute("SELECT activity_id FROM runs").fetchall()}
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        end_date = datetime.now().strftime("%Y-%m-%d")
        log(f"📥 撈取 {start_date} ~ {end_date} 的跑步紀錄...")
        activities = api.get_activities_by_date(start_date, end_date, "running")
        new_count = 0
        for act in activities:
            if act.get("activityType", {}).get("typeKey") != "running":
                continue
            aid = act.get("activityId")
            if aid in existing:
                continue
            run = {
                "activity_id": aid,
                "date": act.get("startTimeLocal", "")[:10],
                "name": act.get("activityName", "跑步"),
                "distance_km": round(act.get("distance", 0) / 1000, 2),
                "duration_mins": round(act.get("duration", 0) / 60, 1),
                "elevation_gain_m": round(act.get("elevationGain", 0), 1),
                "avg_pace": _parse_pace(act.get("averageSpeed", 0)),
                "avg_hr": act.get("averageHR"), "max_hr": act.get("maxHR"),
                "avg_cadence": act.get("averageRunningCadenceInStepsPerMinute"),
                "training_effect": act.get("aerobicTrainingEffect"),
                "anaerobic_effect": act.get("anaerobicTrainingEffect"),
            }
            conn.execute("""INSERT OR IGNORE INTO runs VALUES
                (:activity_id,:date,:name,:distance_km,:duration_mins,
                 :elevation_gain_m,:avg_pace,:avg_hr,:max_hr,
                 :avg_cadence,:training_effect,:anaerobic_effect)""", run)
            try:
                splits = api.get_activity_splits(aid)
                for i, lap in enumerate(splits.get("lapDTOs", []), 1):
                    conn.execute("""INSERT INTO activity_splits
                        (activity_id,lap,distance_km,duration_mins,avg_pace,
                         avg_hr,max_hr,avg_cadence,elevation_gain_m)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (
                        aid, i,
                        round(lap.get("distance", 0) / 1000, 3),
                        round(lap.get("duration", 0) / 60, 2),
                        _parse_pace(lap.get("averageSpeed", 0)),
                        lap.get("averageHR"), lap.get("maxHR"),
                        lap.get("averageRunCadence"),
                        round(lap.get("elevationGain", 0) or 0, 1),
                    ))
                log(f"  ✅ {run['date']} {run['name']}")
            except Exception as e:
                log(f"  ⚠️ 圈數資料失敗: {e}")
            new_count += 1
        conn.commit()
        conn.close()
        log(f"🎉 新增 {new_count} 筆，同步完成")

    return run_job(job)

@app.route("/api/analyze", methods=["POST"])
def analyze():
    from flask import request as flask_req
    cfg = flask_req.get_json(silent=True) or {}

    def job(log, emit_event=None):
        import requests as req
        from dotenv import load_dotenv
        import json as _json, datetime

        exe_dir = os.path.dirname(sys.executable) if _IS_FROZEN else os.path.join(_HERE, '..')
        load_dotenv(os.path.join(exe_dir, '.env'))
        API_KEY = os.getenv("OPENROUTER_API_KEY")
        if not API_KEY:
            log("❌ 請在 .env 設定 OPENROUTER_API_KEY"); return

        # ── 解析設定 ──────────────────────────────────────
        coach = cfg.get("coach", "daniels")
        lookback_months = cfg.get("lookback_months", 3)
        rest_days = cfg.get("rest_days", [1, 5])
        lsd_days  = cfg.get("lsd_days",  [0])
        note      = cfg.get("note", "").strip()
        lang      = cfg.get("lang", "zh")
        race_date = (cfg.get("race_date") or "").strip()
        race_type = (cfg.get("race_type") or "").strip()
        race_goal = (cfg.get("race_goal") or "").strip()

        # 賽事類型顯示名稱
        race_type_names = {
            "5K":   ("5 公里 (5K)",            "5K"),
            "10K":  ("10 公里 (10K)",          "10K"),
            "Half": ("半程馬拉松 (Half Marathon, 21.1K)", "Half Marathon (21.1K)"),
            "Full": ("全程馬拉松 (Full Marathon, 42.195K)", "Full Marathon (42.195K)"),
        }
        # 計算距離賽事天數 / 週數
        weeks_to_race = None
        if race_date:
            try:
                _rd = datetime.datetime.strptime(race_date, "%Y-%m-%d").date()
                _days = (_rd - datetime.date.today()).days
                if _days >= 0:
                    weeks_to_race = _days / 7.0
            except ValueError:
                pass

        # 解析目標完賽時間 (HH:MM:SS) 並依賽事距離推算目標配速 (sec/km)
        race_distances_km = {"5K": 5.0, "10K": 10.0, "Half": 21.0975, "Full": 42.195}
        goal_seconds = None
        goal_pace_str = None  # "m:ss/km"
        if race_goal:
            _parts = race_goal.split(":")
            try:
                if len(_parts) == 3:
                    h, m, s = (int(x) for x in _parts)
                    goal_seconds = h * 3600 + m * 60 + s
                elif len(_parts) == 2:
                    m, s = (int(x) for x in _parts)
                    goal_seconds = m * 60 + s
            except ValueError:
                goal_seconds = None
        if goal_seconds and race_type in race_distances_km:
            _pace = goal_seconds / race_distances_km[race_type]  # sec/km
            goal_pace_str = f"{int(_pace // 60)}:{int(round(_pace % 60)):02d}"

        # 根據月份計算開始日期
        if lookback_months > 0:
            start_date = (datetime.datetime.now() - datetime.timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")
            runs = query("SELECT * FROM runs WHERE date >= ? ORDER BY date DESC", (start_date,))
            time_desc = f"最近 {lookback_months} 個月 (自 {start_date} 起)"
        else:
            runs = query("SELECT * FROM runs ORDER BY date DESC")
            time_desc = "全部歷史紀錄"

        if not runs:
            log(f"❌ {time_desc} 內無跑步紀錄！"); return
        log(f"✅ 載入 {len(runs)} 筆跑步紀錄 ({time_desc})，呼叫 AI 中...")

        # ── AI 呼叫基礎設施（供 auto 選教練與主排課共用）────────────
        # 依序嘗試的模型：主要免費模型失敗時 fallback 到其他免費模型
        # 可用環境變數 OPENROUTER_MODELS（逗號分隔）覆寫
        models_env = os.getenv("OPENROUTER_MODELS", "").strip()
        if models_env:
            models = [m.strip() for m in models_env.split(",") if m.strip()]
        else:
            models = [
                "nvidia/nemotron-3-super-120b-a12b:free",
                "nvidia/nemotron-3-ultra-550b-a55b:free",
                "cohere/north-mini-code:free",
                "dots-studio/dots-3-note-preview:free",
                "poolside/laguna-s-2.1:free",
                "inclusionai/ling-3.0-flash-sante:free",
            ]

        import time as _time

        def call_ai(prompt_text, model):
            """對單一 model 呼叫，遇 429/5xx 以指數退避重試。回傳 (content, None) 或 (None, err_msg)。"""
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    resp = req.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {API_KEY}",
                                 "Content-Type": "application/json"},
                        json={"model": model,
                              "messages": [{"role": "user", "content": prompt_text}]},
                        timeout=120
                    )
                except Exception as e:
                    return None, f"連線錯誤: {e}"

                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except Exception as e:
                        return None, f"回應解析失敗: {e} / {resp.text[:200]}"
                    # OpenRouter 有時回 HTTP 200 但 body 內含 error（如上游 503/429 overloaded）
                    if isinstance(body, dict) and body.get("error"):
                        err = body["error"]
                        ecode = err.get("code")
                        emsg = err.get("message", "")[:150]
                        # 上游暫時性錯誤（429/5xx / overloaded）→ 退避重試
                        transient = ecode in (429, 500, 502, 503, 504) or \
                            (err.get("metadata", {}) or {}).get("error_type") == "provider_overloaded"
                        if transient and attempt < max_retries - 1:
                            wait = 2 ** attempt
                            log(f"⏳ {model} 上游錯誤 {ecode}，{wait}s 後重試 "
                                f"({attempt + 1}/{max_retries - 1})...")
                            _time.sleep(wait)
                            continue
                        return None, f"{ecode}: {emsg}"
                    try:
                        return body["choices"][0]["message"]["content"], None
                    except Exception as e:
                        return None, f"回應解析失敗: {e} / {resp.text[:200]}"

                # 429（限流）或 5xx（上游暫時性錯誤）→ 退避後重試
                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt < max_retries - 1:
                        wait = 2 ** attempt  # 1s, 2s, 4s
                        log(f"⏳ {model} 回 {resp.status_code}，{wait}s 後重試 "
                            f"({attempt + 1}/{max_retries - 1})...")
                        _time.sleep(wait)
                        continue
                    return None, f"{resp.status_code}: {resp.text[:200]}"

                # 其他錯誤（4xx）不重試，直接放棄此 model
                return None, f"{resp.status_code}: {resp.text[:200]}"
            return None, "重試次數用盡"

        def run_with_fallback(prompt_text):
            """依序嘗試 models，成功回傳 content，全部失敗回傳 None。"""
            last_err = ""
            for i, model in enumerate(models):
                if i > 0:
                    log(f"🔀 切換備援模型: {model}")
                content, err = call_ai(prompt_text, model)
                if content is not None:
                    if i > 0:
                        log(f"✅ 使用備援模型 {model} 成功")
                    return content
                last_err = err
                log(f"⚠️ {model} 失敗: {err}")
            log(f"❌ 所有模型皆呼叫失敗，最後錯誤: {last_err}")
            log("💡 免費模型常因上游限流回 429，請稍候再試，或於 .env 設定 "
                "OPENROUTER_MODELS 指定其他模型 / 使用付費模型。")
            return None

        # ── Auto：不套固定流派，依「跑者既有訓練框架」個人化規劃 ──────
        if coach == "auto":
            log("🤖 Auto 模式：分析你的訓練框架，進行個人化規劃中...")
            all_runs = query("SELECT max_hr FROM runs WHERE max_hr IS NOT NULL")
            hrmax = estimate_hrmax(all_runs) or 190
            # 目標 MP（秒/km）：優先用使用者填的完賽時間換算，否則用預設 4:02
            if goal_seconds and race_type in race_distances_km:
                mp_sec = goal_seconds / race_distances_km[race_type]
            else:
                mp_sec = 4 * 60 + 2
            fw, _buckets = build_framework(runs, mp_sec, hrmax)
            anchors = pace_anchors(mp_sec)
            interval_prog = detect_interval_progression(runs)
            if interval_prog:
                log(f"🔁 間歇週期：{interval_prog['summary']}")

            def _rng(t):
                return f"{_sec_to_pace_str(t[0])}~{_sec_to_pace_str(t[1])}" if t else "資料不足"
            def _hr(t):
                return f"{t[0]}~{t[1]} bpm" if t else "資料不足"

            log(f"📊 推估 HRmax≈{hrmax}；目標 MP≈{_sec_to_pace_str(mp_sec)}/km；"
                f"慣用 easy≈{fw['easy_dist_med'] or '?'}km、long≈{fw['long_dist_med'] or '?'}km、"
                f"週里程≈{fw['week_km_med']:.0f}km、質量課≈{fw['quality_per_week']:.1f}堂/週")

            framework_block = f"""【這位跑者「既有的訓練框架」(由歷史數據加權分析得出，心率0.40+距離0.35+配速0.25)】
* 推估 HRmax：{hrmax} bpm（取自歷史 max_hr 高位穩健值，請「勿」擅自更改此數值）
* 慣用輕鬆跑(Easy)距離：中位 {fw['easy_dist_med'] or '?'} km，範圍 {fw['easy_dist_range']}
* 慣用長跑(Long)距離：中位 {fw['long_dist_med'] or '?'} km，範圍 {fw['long_dist_range']}
* 常態週里程：中位約 {fw['week_km_med']:.0f} km
* 質量負荷頻率：約每週 {fw['quality_per_week']:.1f} 堂（含間歇、節奏跑「與質量長跑」）
* 跑者個人各區實際心率：輕鬆/有氧長跑 {_hr(fw['easy_hr'])}；質量課 {_hr(fw['quality_hr'])}

【配速錨點——請嚴格分兩類設定，不要自行用 VDOT 公式亂算】
▍有氧課「用跑者實測配速」為準（因為這反映其當下真實有氧狀態）：
* 輕鬆跑(Easy)：{_rng(fw['easy_pace_range'])} /km（來自歷史「心率落在有氧區」的輕鬆跑實測）
* 有氧長跑(Long)：{_rng(fw['long_pace_range'])} /km（來自歷史「有氧長跑」實測，「不含」快長跑/質量長跑）
▍質量課「用目標 MP 反推」為準（因為這是為達標所需的前瞻強度）：
* 馬拉松配速跑(M)：{_sec_to_pace_str(anchors['M'][0])} /km
* 節奏跑(T)：{_rng(anchors['T'])} /km
* 長間歇(>=1600m)：{_rng(anchors['long_interval'])} /km
* 短間歇(<1600m)：{_rng(anchors['short_interval'])} /km"""

            structure_rules = """【課表編排硬規則(務必遵守)】
1. 尊重跑者既有訓練量：Easy 課距離應貼近其慣用值(勿大幅縮短，例如不要把慣用 16km 的 easy 開成 10km)；Long 課距離沿用其慣用範圍。
2. 質量課(節奏跑/長間歇/短間歇/質量長跑)之間至少間隔一天 Easy 或休息日，不可連續兩天安排質量課；安排質量長跑當天亦視為一堂質量課。
3. 嚴格遵守使用者指定的「固定休息日」與「LSD 長跑日」，不得在休息日排跑步。
4. 週里程與跑者常態相當(±15%以內)，除非有明顯過量或傷害風險才調整，並須說明理由。
5. 有氧課(Easy/有氧長跑)一律採用上方「實測配速錨點」；質量課採用上方「MP 反推錨點」。全部用跑者「個人各區實際心率」交叉標註；不得竄改 HRmax 或歷史數據。
6. 訓練學正確觀念：輕鬆跑「本來就應該」比馬拉松配速慢約 50~90 秒/km（對應約 65~79% HRmax、能正常對話的強度），這是「正確且刻意的設計」，其生理目的是有氧基礎、粒線體與微血管適應及恢復。「嚴禁」把「輕鬆跑配速慢於 MP」當成缺點或不足，也「不得」建議跑者把輕鬆跑加速接近 MP。輕鬆跑跑太快反而破壞恢復與有氧適應。"""

            race_line = ""
            if race_type in race_type_names:
                race_line = f"\n* 目標賽事：{race_type_names[race_type][0]}"
                if race_date:
                    race_line += f"，賽事日期 {race_date}"
                    if weeks_to_race is not None:
                        race_line += f"（距今約 {weeks_to_race:.1f} 週）"
                if race_goal:
                    race_line += f"；目標完賽 {race_goal}（MP≈{_sec_to_pace_str(mp_sec)}/km）"
            note_block = f"\n【跑者補充訊息】\n{note}" if note else ""
            day_names_zh = ["週日","週一","週二","週三","週四","週五","週六"]
            rest_s = "、".join(day_names_zh[d] for d in rest_days) if rest_days else "無"
            lsd_s  = "、".join(day_names_zh[d] for d in lsd_days)  if lsd_days  else "無"

            interval_block = ""
            if interval_prog:
                stage_zh = "長間歇" if interval_prog["stage"] == "long" else "短間歇"
                rmax = interval_prog["recent_max"]
                interval_block = f"""
【間歇訓練週期進程（重要，請延續而非倒退）】
* {interval_prog['summary']}
* 這位跑者目前的間歇訓練「已進入{stage_zh}階段」，近期已達到的最大單趟距離為 {rmax}m。
* 「除非跑者在補充訊息中明確要求降低/縮短」，否則下週間歇課的「單趟距離不得低於 {rmax}m」，且不得倒退回短間歇(<1600m)；應維持或往更專項方向推進。"""
                if interval_prog["stage"] == "long":
                    interval_block += (f"可安排長間歇(單趟>={rmax}m，例如維持 {rmax}m 並增加組數、"
                                       "漸進至更長單趟、或縮短恢復)，或依賽事臨近程度轉為馬拉松配速跑的專項課。")
                else:
                    interval_block += "可延續短間歇並視進度逐步拉長單趟距離，往長間歇推進。"
                if note:
                    interval_block += "（注意：若上方跑者補充訊息有特別指示間歇安排，以其要求為準。）"

            auto_prompt = f"""你是一位頂尖的個人化馬拉松教練。你「不套用任何固定訓練流派(不強行套 Daniels/Hansons/Lydiard)」，而是「尊重並沿用這位跑者既有的訓練框架」，只針對其目標賽事做配速校準、強度分配與賽前週期化調整。

【跑者目標】{race_line}
* 固定休息日：{rest_s}；LSD 長跑日：{lsd_s}{note_block}

{framework_block}

{structure_rules}
{interval_block}

{TERMINOLOGY_ZH}

【跑步歷史數據 (JSON - {time_desc})】
{_json.dumps(runs, ensure_ascii=False, indent=2)}

【請執行以下任務，全程使用繁體中文】
1. 訓練框架診斷：用上述框架與個人心率，說明這位跑者目前的訓練型態與優缺點（配速/心率/量的關係）。
2. 配速區間：直接採用上方「配速錨點」，並標註各區對應的「個人實際心率」。
3. 下週課表：以「尊重既有訓練量」為原則編排(Easy 貼近慣用距離、Long 沿用慣用範圍)，嚴格遵守休息日/LSD日與課表編排硬規則，並針對目標賽事做適當的強度與週期化調整。若有任何調整既有量之處，需明確說明理由。
4. 定稿前自我檢查：Easy/有氧長跑是否慢於 MP、質量課(含質量長跑)是否未連續兩天、是否遵守休息日、Easy 距離是否貼近慣用值、間歇課單趟是否不低於近期最大值(除非跑者要求)且未倒退回短間歇、診斷中是否「未」把「輕鬆跑慢於 MP」誤列為缺點。若違反請修正後再輸出。"""

            content = run_with_fallback(auto_prompt)
            if content is None:
                return
            content = content.replace(r"\&", "&").replace(r"\~", "~").replace(r"\text{", "").replace("}", "")
            content = normalize_terms(content)
            with open(AI_PLAN_FILE, "w", encoding="utf-8") as f:
                _json.dump({"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "coach": "auto", "content": content}, f, ensure_ascii=False, indent=2)
            log("💾 課表已儲存至 ai_plan.json")
            log("✅ AI 個人化分析完成，請重新整理頁面查看課表。")
            return

        if lang == "en":
            day_names = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
            rest_str = ", ".join(day_names[d] for d in rest_days) if rest_days else "none"
            lsd_str  = ", ".join(day_names[d] for d in lsd_days)  if lsd_days  else "none"
            coach_desc = {
                "daniels":  "Jack Daniels' Running Formula (E/M/T/I/R pace zones)",
                "hansons":  "Hansons Marathon Method (cumulative fatigue, SOS workouts, never 20-mile long run)",
                "lydiard":  "Lydiard Periodization (aerobic base → hill phase → track phase → racing)",
            }.get(coach, "Jack Daniels' Running Formula")
            coach_rules = {
                "daniels": """[Jack Daniels — Pace Zone Definitions (derive from VDOT, cross-check with the athlete's actual HR/pace data; do NOT just add/subtract fixed seconds from MP)]
* E (Easy): ~MP + 0:60 to 1:30 per km. 65-79% HRmax. Base/recovery/warm-up.
* M (Marathon): = goal MP. 80-89% HRmax.
* T (Threshold/Tempo): faster than MP by only ~0:10-0:20 per km — pace you can hold ~1 hour ("comfortably hard"). 88-92% HRmax. Tempo runs (20 min) or cruise intervals.
* Long Interval (>=1600m, VO2max): faster than T. 3-5 min reps at ~95-100% HRmax.
* Short Interval (<1600m, speed): fastest, short reps (200-1000m) for speed/economy; not HR-driven.
STRICT ordering (slow→fast): Easy > Marathon Pace > Tempo > Long Interval > Short Interval.""",
                "hansons": """[Hansons — Pace Zone Definitions (KEY: Hansons "tempo" = Marathon Pace, NOT faster than MP)]
* Easy: MP + 1:00 to 2:00 per km. 60-70% HRmax. Most weekly volume runs here.
* Tempo run (Hansons SOS): run AT Marathon Pace (= goal MP, e.g. 4:04/km). This is the signature workout, building up toward ~16 km at MP. Do NOT make it faster than MP.
* Strength (SOS): slightly faster than MP by ~0:06-0:10 per km only (e.g. ~10K-to-HM effort), simulating late-race fatigue.
* Speed (SOS, early cycle): 5K-3K race effort intervals, faster than strength.
* Long run: run at EASY pace (same zone as easy days, NOT faster than easy), never exceed ~16 miles (~26 km); rely on cumulative fatigue, not distance.
STRICT ordering (slow→fast): Long ≈ Easy > Tempo(=MP) > Strength > Speed.
Common mistake to AVOID: setting Tempo faster than MP, or making the Long run faster than Easy.""",
                "lydiard": """[Lydiard — Pace Zone Definitions (aerobic-base first, drive by feel/HR more than fixed pace math)]
* Aerobic/base runs: comfortable aerobic effort, ~MP + 0:60 to 1:45 per km, well below anaerobic threshold. 70-80% HRmax. This is the bulk of base phase.
* Steady/aerobic threshold: near but below threshold, ~MP + 0:10-0:30 per km.
* Hill phase: hill bounding/springing for strength, effort-based, not a road pace.
* Anaerobic/track phase (later): intervals & time trials faster than MP for sharpening.
* Long run: aerobic effort, same easy zone (NOT faster than easy).
STRICT ordering (slow→fast): Long ≈ Aerobic base > Steady > MP > Track intervals.
Prioritize aerobic development; do not prescribe fast paces during the base phase.""",
            }.get(coach, "")
            note_section = f"\n[Runner's Notes]\n{note}" if note else ""
            race_section = ""
            if race_type in race_type_names:
                race_en = race_type_names[race_type][1]
                race_section = f"\n* Target race: {race_en}"
                if race_date:
                    race_section += f", scheduled on {race_date}"
                    if weeks_to_race is not None:
                        race_section += f" (~{weeks_to_race:.1f} weeks away — periodize the plan to peak for this date)"
                if race_goal:
                    race_section += f"\n* Goal finish time: {race_goal}"
                    if goal_pace_str:
                        race_section += f" (required race pace ~{goal_pace_str}/km — align workout paces to this goal)"
            prompt = f"""You are an elite marathon coach specializing in "{coach_desc}".

[Athlete's Goal]
* Goal: Break Sub 2:54 marathon (target pace ~4:04/km) in the second half of this year.{race_section}
* Fixed rest days: {rest_str} (no running on these days)
* LSD long run days: {lsd_str} (long easy runs scheduled on these days){note_section}

[Running History Data (JSON - Limited to {time_desc})]
{_json.dumps(runs, ensure_ascii=False, indent=2)}

{coach_rules}

{TERMINOLOGY_EN}

[Tasks — follow {coach_desc} philosophy and the pace-zone definitions above]
1. Fitness & fatigue diagnosis: analyze the relationship between average HR and pace in the provided data.
2. Calculate training pace zones based on goal MP ({goal_pace_str or '4:04'}/km) AND the definitions above. Cross-check against the athlete's real HR/pace data — do not mechanically add/subtract fixed seconds.
3. Before finalizing, VERIFY the paces obey the STRICT ordering stated in the definitions (e.g. Easy/Long must be slower than MP; for Hansons the Tempo run equals MP and the Long run is not faster than Easy). If any zone violates the ordering, fix it.
4. Build next week's training plan.
"""
        else:
            day_names = ["週日","週一","週二","週三","週四","週五","週六"]
            rest_str = "、".join(day_names[d] for d in rest_days) if rest_days else "無"
            lsd_str  = "、".join(day_names[d] for d in lsd_days)  if lsd_days  else "無"
            coach_desc = {
                "daniels":  "Jack Daniels 科學化跑步方程式（E/M/T/I/R 配速區間）",
                "hansons":  "Hansons 馬拉松訓練法（累積疲勞、SOS 課、Never 20 miles long run）",
                "lydiard":  "Lydiard 週期化訓練（有氧基礎→山坡強化→田徑期→賽季）",
            }.get(coach, "Jack Daniels 科學化跑步方程式")
            coach_rules = {
                "daniels": """【Jack Daniels — 配速區間定義（請由 VDOT 推算，並用跑者實際心率/配速交叉驗證；不要只用「MP 加減固定秒數」硬套）】
* E 輕鬆跑（Easy）：約 MP + 0:60 ～ 1:30/km。65-79% HRmax。基礎、恢復、熱身/冷卻。
* M 馬拉松配速（Marathon）：= 目標 MP。80-89% HRmax。
* T 節奏/閾值跑（Threshold/Tempo）：只比 MP 快約 0:10 ～ 0:20/km，是「可維持約 1 小時」的「舒適的辛苦」配速。88-92% HRmax。20 分鐘 tempo 或巡航間歇。
* 長間歇（>=1600m，VO2max）：比節奏跑更快，3-5 分鐘反覆，約 95-100% HRmax。
* 短間歇（<1600m，速度）：最快，200-1000m 短反覆，練速度/跑步經濟性；不以心率為準。
嚴格由慢到快：輕鬆跑 ＞ 馬拉松配速跑 ＞ 節奏跑 ＞ 長間歇 ＞ 短間歇。""",
                "hansons": """【Hansons — 配速區間定義（重點：Hansons 的「tempo」＝馬拉松配速，不是比 MP 快！）】
* 輕鬆跑（Easy）：MP + 1:00 ～ 2:00/km。60-70% HRmax。每週大部分里程都在此。
* Tempo 跑（Hansons 招牌 SOS 課）：就以「馬拉松配速」跑（＝目標 MP，例如 4:04/km），逐步累積到約 16 km @ MP。絕對不要比 MP 快。
* 力量課（Strength, SOS）：只比 MP 快約 0:06 ～ 0:10/km（約 10K～半馬強度），模擬比賽後段疲勞。
* 速度課（Speed, SOS，週期早段）：5K-3K 比賽強度的間歇，比力量課快。
* 長跑（Long Run）：以「輕鬆跑配速」進行（與 easy 同區間，不得比 easy 快），距離不超過約 16 英里（約 26 km）；靠累積疲勞而非距離。
嚴格由慢到快：長跑 ≈ 輕鬆跑 ＞ Tempo(＝MP) ＞ 力量課 ＞ 速度課。
必須避免的常見錯誤：把 Tempo 設得比 MP 快；或把長跑配速設得比輕鬆跑還快。""",
                "lydiard": """【Lydiard — 配速區間定義（有氧基礎優先，多依感覺/心率而非固定配速公式）】
* 有氧/基礎跑（Aerobic base）：舒適有氧強度，約 MP + 0:60 ～ 1:45/km，明顯低於無氧閾值。70-80% HRmax。基礎期的主體。
* 穩定跑/有氧閾值（Steady）：接近但低於閾值，約 MP + 0:10 ～ 0:30/km。
* 山坡期（Hill phase）：以山坡跳躍/彈跳練力量，依強度而非公路配速。
* 無氧/田徑期（後段）：比 MP 快的間歇與計時跑，做最後銳化。
* 長跑（Long Run）：有氧強度，與輕鬆跑同區間（不得比 easy 快）。
嚴格由慢到快：長跑 ≈ 有氧基礎 ＞ 穩定跑 ＞ MP ＞ 田徑間歇。
基礎期以有氧發展為優先，不要在基礎期就開很快的配速。""",
            }.get(coach, "")
            note_section = f"\n【跑者補充訊息】\n{note}" if note else ""
            race_section = ""
            if race_type in race_type_names:
                race_zh = race_type_names[race_type][0]
                race_section = f"\n* 目標賽事：{race_zh}"
                if race_date:
                    race_section += f"，賽事日期 {race_date}"
                    if weeks_to_race is not None:
                        race_section += f"（距今約 {weeks_to_race:.1f} 週，請以此日期為高峰做週期化編排）"
                if race_goal:
                    race_section += f"\n* 目標完賽時間：{race_goal}"
                    if goal_pace_str:
                        race_section += f"（換算目標配速約 {goal_pace_str}/km，請以此配速為基準設定各項課表配速）"
            prompt = f"""
你是一位精通「{coach_desc}」的國家級馬拉松教練。

【使用者當前目標】
* 目標：今年下半年挑戰全程馬拉松突破 Sub 2:54（目標配速約為 4:04/km）。{race_section}
* 固定休息日：{rest_str}（這幾天不安排任何跑步訓練）
* LSD 長跑日：{lsd_str}（這幾天安排長距離慢跑）{note_section}

【跑步歷史數據 (JSON 格式 - 僅限 {time_desc})】
{_json.dumps(runs, ensure_ascii=False, indent=2)}

{coach_rules}

{TERMINOLOGY_ZH}

【請依照 {coach_desc} 的訓練哲學「以及上述配速區間定義」，執行以下任務】
1. 體能與疲勞診斷：針對「提供的資料範圍內」分析最近幾次跑步的「平均心率與配速關係」。
2. 計算訓練配速區間：以目標 MP（{goal_pace_str or '4:04'}/km）「並依上述定義」設定各區間，同時用跑者實際心率/配速交叉驗證；不要機械式地加減固定秒數。
3. 定稿前請「自我檢查」各配速是否符合上述定義的嚴格由慢到快順序（例如：輕鬆跑/長跑必須慢於 MP；Hansons 的 Tempo 課＝MP、長跑不得快於輕鬆跑）。若有任何區間違反順序，請修正後再輸出。
4. 編排下週動態訓練課表。
"""
        content = run_with_fallback(prompt)
        if content is None:
            return
        # 清理 AI 可能輸出的 LaTeX 轉義字元，避免 MathJax 渲染錯誤
        content = content.replace(r"\&", "&").replace(r"\~", "~").replace(r"\text{", "").replace("}", "")
        content = normalize_terms(content)
        
        with open(AI_PLAN_FILE, "w", encoding="utf-8") as f:
            _json.dump({"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "coach": coach, "content": content}, f, ensure_ascii=False, indent=2)
        log("💾 課表已儲存至 ai_plan.json")
        log("✅ AI 分析完成，請重新整理頁面查看課表。")

    return run_job(job)

if __name__ == "__main__":
    app.run(debug=False, port=5000)
