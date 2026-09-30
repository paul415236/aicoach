#!/usr/bin/env python3
import json
import os
import queue
import sqlite3
import threading
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
* I (Interval, VO2max): faster than T. 3-5 min reps at ~95-100% HRmax.
* R (Repetition): fastest, short reps (200-400m) for speed/economy; not HR-driven.
STRICT ordering (slow→fast): E > M > T > I > R.""",
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
* I 間歇（Interval, VO2max）：比 T 更快，3-5 分鐘反覆跑，約 95-100% HRmax。
* R 反覆跑（Repetition）：最快，200-400m 短反覆，練速度/跑步經濟性；不以心率為準。
嚴格由慢到快：E ＞ M ＞ T ＞ I ＞ R。""",
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

【請依照 {coach_desc} 的訓練哲學「以及上述配速區間定義」，執行以下任務】
1. 體能與疲勞診斷：針對「提供的資料範圍內」分析最近幾次跑步的「平均心率與配速關係」。
2. 計算訓練配速區間：以目標 MP（{goal_pace_str or '4:04'}/km）「並依上述定義」設定各區間，同時用跑者實際心率/配速交叉驗證；不要機械式地加減固定秒數。
3. 定稿前請「自我檢查」各配速是否符合上述定義的嚴格由慢到快順序（例如：輕鬆跑/長跑必須慢於 MP；Hansons 的 Tempo 課＝MP、長跑不得快於輕鬆跑）。若有任何區間違反順序，請修正後再輸出。
4. 編排下週動態訓練課表。
"""
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

        def call_ai(model):
            """對單一 model 呼叫，遇 429/5xx 以指數退避重試。回傳 (content, None) 或 (None, err_msg)。"""
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    resp = req.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {API_KEY}",
                                 "Content-Type": "application/json"},
                        json={"model": model,
                              "messages": [{"role": "user", "content": prompt}]},
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

        content = None
        last_err = ""
        for i, model in enumerate(models):
            if i > 0:
                log(f"🔀 切換備援模型: {model}")
            content, err = call_ai(model)
            if content is not None:
                if i > 0:
                    log(f"✅ 使用備援模型 {model} 成功")
                break
            last_err = err
            log(f"⚠️ {model} 失敗: {err}")

        if content is None:
            log(f"❌ 所有模型皆呼叫失敗，最後錯誤: {last_err}")
            log("💡 免費模型常因上游限流回 429，請稍候再試，或於 .env 設定 "
                "OPENROUTER_MODELS 指定其他模型 / 使用付費模型。")
            return
        # 清理 AI 可能輸出的 LaTeX 轉義字元，避免 MathJax 渲染錯誤
        content = content.replace(r"\&", "&").replace(r"\~", "~").replace(r"\text{", "").replace("}", "")
        
        with open(AI_PLAN_FILE, "w", encoding="utf-8") as f:
            _json.dump({"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "coach": coach, "content": content}, f, ensure_ascii=False, indent=2)
        log("💾 課表已儲存至 ai_plan.json")
        log("✅ AI 分析完成，請重新整理頁面查看課表。")

    return run_job(job)

if __name__ == "__main__":
    app.run(debug=False, port=5000)
