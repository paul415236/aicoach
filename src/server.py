#!/usr/bin/env python3
import os
# 停用 pydantic 外掛（garth→logfire 的 pydantic plugin 在打包成 exe 時會因
# inspect.getsource 讀不到原始碼而崩潰）。須在 import pydantic 相關之前設定。
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "true")

import json
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
AI_PLAN_HISTORY_FILE = os.path.join(DATA_DIR, "ai_plan_history.json")


def ensure_schema(conn=None):
    """建立所有資料表（若不存在）。可傳入既有連線，否則自行開關。"""
    own = conn is None
    if own:
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
    # 單次訓練 AI 跑力分析的快取
    conn.execute("""CREATE TABLE IF NOT EXISTS run_analysis (
        activity_id INTEGER PRIMARY KEY, content TEXT,
        generated_at TEXT, lang TEXT)""")
    conn.commit()
    if own:
        conn.close()

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


def tr(lang, zh, en):
    """依語言回傳對應字串；lang=='en' 用 en，否則用 zh。"""
    return en if lang == "en" else zh


# AI prompt 素材與術語處理已抽出至 prompts.py
import prompts
from prompts import TERMINOLOGY_ZH, TERMINOLOGY_EN, normalize_terms

# AI 呼叫（OpenRouter 重試 + fallback）已抽出至 ai_client.py
import ai_client

# 分類演算法已抽出至 classifier.py
from classifier import (
    _pace_str_to_sec, _sec_to_pace_str, estimate_hrmax,
    _pace_zone_by_mp, _hr_zone, classify_run, build_framework,
    pace_anchors, hr_zones_by_pct, detect_interval_progression,
    monthly_type_summary, map_category,
)





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
    resp = send_from_directory(BASE_DIR, "dashboard.html")
    # 本機工具：避免瀏覽器快取到舊版前端（改版後 F5 就能看到最新）
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/runs")
def get_runs():
    return json_resp(query("SELECT * FROM runs ORDER BY date DESC"))

@app.route("/api/runs/<int:activity_id>/splits")
def get_splits(activity_id):
    return json_resp(query(
        "SELECT * FROM activity_splits WHERE activity_id=? ORDER BY lap",
        (activity_id,)
    ))

def _is_tempo_run(r):
    """與 build_framework 內一致的節奏跑名稱判斷。"""
    n = r.get("name") or ""
    return any(x in n for x in ["Tempo", "tempo", "LT", "節奏"])


def _estimate_mp_sec():
    """推估目標馬拉松配速 (秒/km)。
    優先讀使用者在 AI 分析設定存下的目標完賽時間；否則用預設 4:02/km。
    與 analyze() auto 模式的 mp_sec 推估邏輯一致。"""
    race_distances_km = {"5K": 5.0, "10K": 10.0, "Half": 21.0975, "Full": 42.195}
    default_mp = 4 * 60 + 2  # 4:02/km
    if not os.path.exists(ANALYZE_CONFIG_FILE):
        return default_mp
    try:
        with open(ANALYZE_CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return default_mp
    race_type = (cfg.get("race_type") or "").strip()
    race_goal = (cfg.get("race_goal") or "").strip()
    if race_goal and race_type in race_distances_km:
        parts = race_goal.split(":")
        try:
            if len(parts) == 3:
                h, m, s = (int(x) for x in parts)
                goal_sec = h * 3600 + m * 60 + s
            elif len(parts) == 2:
                m, s = (int(x) for x in parts)
                goal_sec = m * 60 + s
            else:
                return default_mp
            return goal_sec / race_distances_km[race_type]
        except (ValueError, ZeroDivisionError):
            return default_mp
    return default_mp


def _fmt_monthly_summary(summ, lang):
    """把 monthly_type_summary 的結果組成 prompt 用的文字表。"""
    names = {"easy": ("輕鬆跑", "Easy"), "tempo": ("節奏跑", "Tempo"),
             "interval": ("間歇跑", "Interval"), "long": ("長跑", "Long")}
    lines = []
    for c in ["easy", "tempo", "interval", "long"]:
        s = summ.get(c, {})
        if not s.get("count"):
            continue
        nm = names[c][1 if lang == "en" else 0]
        if lang == "en":
            lines.append(f"- {nm}: {s['count']} runs, {s['total_km']} km total, "
                         f"avg pace {s['avg_pace'] or '--'}/km, "
                         f"avg HR {s['avg_hr'] or '--'}, max HR {s['max_hr'] or '--'}")
        else:
            lines.append(f"- {nm}：{s['count']} 次、總量 {s['total_km']} km、"
                         f"平均配速 {s['avg_pace'] or '--'}/km、"
                         f"平均心率 {s['avg_hr'] or '--'}、最大心率 {s['max_hr'] or '--'}")
    if not lines:
        return tr(lang, "（近一月資料不足）", "(insufficient data in the last month)")
    return "\n".join(lines)


def _build_run_analysis_prompt(run, splits, monthly, mp_sec, hrmax, lang):
    """組單次訓練 AI 跑力分析的 prompt。"""
    import json as _json
    mp_str = _sec_to_pace_str(mp_sec)
    monthly_block = _fmt_monthly_summary(monthly, lang)
    terms = TERMINOLOGY_EN if lang == "en" else TERMINOLOGY_ZH
    run_json = _json.dumps(run, ensure_ascii=False, indent=2)
    splits_json = _json.dumps(splits, ensure_ascii=False, indent=2)
    if lang == "en":
        return f"""You are an elite running coach. Analyze ONE single training session objectively, using the runner's last-30-day baselines as reference. Respond entirely in English, in concise markdown.

[The session to analyze (JSON)]
{run_json}

[Lap splits of this session (JSON)]
{splits_json}

[Runner's last-30-day baselines by type (reference for objectivity)]
{monthly_block}
Estimated HRmax: {hrmax} bpm. Goal marathon pace (MP) ≈ {mp_str}/km.

{terms}

[Tasks — cover these five points, each as a short section]
1. Session type & whether the intensity was appropriate (compare pace/HR to the baselines above).
2. Pace–HR relationship: aerobic efficiency and any cardiac drift across the laps.
3. Lap pacing consistency (even / positive / negative split; any fade).
4. VDOT / running-fitness estimate implied by this session, and roughly what race level it maps to.
5. One or two concrete, actionable suggestions.
Keep it concise and specific; refer to real numbers from the data."""
    return f"""你是一位頂尖跑步教練。請「客觀」分析這「單一次」訓練，並以跑者近 30 天的各類型基準作為參照。全程使用繁體中文，以精簡的 markdown 回答。

【要分析的本次訓練（JSON）】
{run_json}

【本次訓練的分圈資料（JSON）】
{splits_json}

【跑者近 30 天各類型基準（供客觀比較）】
{monthly_block}
推估 HRmax：{hrmax} bpm。目標馬拉松配速 (MP) ≈ {mp_str}/km。

{terms}

【請涵蓋以下五點，每點一個小段落】
1. 本次課的性質，以及強度是否恰當（將配速/心率與上方基準比較）。
2. 配速與心率的關係：有氧效率，以及分圈間是否有心率漂移（cardiac drift）。
3. 分圈配速的穩定度（平均配速 / 前快後慢 / 後段加速；是否掉速）。
4. 由本次表現推估的 VDOT / 跑力，大約相當於什麼比賽水準。
5. 一到兩點具體、可執行的建議。
請精簡且具體，引用資料中的實際數字。"""


@app.route("/api/analyze-run/<int:activity_id>", methods=["POST"])
def analyze_run(activity_id):
    from flask import request as freq
    from dotenv import load_dotenv
    cfg = freq.get_json(silent=True) or {}
    lang = cfg.get("lang", "zh")
    force = freq.args.get("force") == "1"

    ensure_schema()

    # 1) 快取：非強制時，有快取直接回
    if not force:
        cached = query("SELECT content, generated_at FROM run_analysis WHERE activity_id=?",
                       (activity_id,))
        if cached:
            return json_resp({"content": cached[0]["content"],
                              "generated_at": cached[0]["generated_at"],
                              "cached": True})

    # 2) 撈該筆 run 與分圈
    rows = query("SELECT * FROM runs WHERE activity_id=?", (activity_id,))
    if not rows:
        return json_resp({"error": tr(lang, "找不到此訓練紀錄", "run not found")})
    run = rows[0]
    splits = query("SELECT lap, distance_km, duration_mins, avg_pace, avg_hr, "
                   "max_hr, avg_cadence, elevation_gain_m FROM activity_splits "
                   "WHERE activity_id=? ORDER BY lap", (activity_id,))

    # 3) 近 30 天各類型摘要
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    recent = query("SELECT * FROM runs WHERE date >= ? ORDER BY date DESC", (cutoff,))
    all_hr = query("SELECT max_hr FROM runs WHERE max_hr IS NOT NULL")
    hrmax = estimate_hrmax(all_hr) or 190
    mp_sec = _estimate_mp_sec()
    monthly = monthly_type_summary(recent, mp_sec, hrmax)

    # 4) API key
    exe_dir = os.path.dirname(sys.executable) if _IS_FROZEN else os.path.join(_HERE, '..')
    load_dotenv(os.path.join(exe_dir, '.env'), override=True)
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return json_resp({"error": tr(lang, "請在 .env 設定 OPENROUTER_API_KEY",
                                      "Please set OPENROUTER_API_KEY in .env")})

    # 5) 組 prompt → AI
    prompt = _build_run_analysis_prompt(run, splits, monthly, mp_sec, hrmax, lang)
    content = ai_client.run_with_fallback(prompt, api_key,
                                          models=ai_client.get_models(), tr=tr, lang=lang)
    if content is None:
        return json_resp({"error": tr(lang, "AI 分析失敗，請稍後再試（免費模型可能限流）",
                                      "AI analysis failed, please retry later (free models may be rate-limited)")})
    content = normalize_terms(content)

    # 6) 存 DB 快取
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("INSERT OR REPLACE INTO run_analysis (activity_id, content, generated_at, lang) "
                 "VALUES (?,?,?,?)", (activity_id, content, generated_at, lang))
    conn.commit()
    conn.close()

    return json_resp({"content": content, "generated_at": generated_at, "cached": False})


@app.route("/api/weekly-stats")
def weekly_stats():
    """每週訓練統計：複用 classify_run 的加權分類，將每筆跑步歸為
    輕鬆跑 / 節奏跑 / 間歇跑 / 長跑 / 其他，按 ISO 週聚合各類的
    時間(分)、跑量(km) 與比例。回傳：
      {
        "mp_sec": <目標MP秒/km>, "hrmax": <推估HRmax>,
        "categories": ["easy","tempo","interval","long","other"],
        "weeks": [ {"week": "2026-W05", "start": "2026-02-02",
                    "total_km": .., "total_mins": ..,
                    "cats": {cat: {"km":.., "mins":.., "km_pct":.., "mins_pct":..}} } ],
        "months": [ {"month": "2026-02", "start": "2026-02-01", ...同上結構... } ],
        "overall": { "total_km":.., "total_mins":..,
                     "cats": {cat: {"km":.., "mins":.., "km_pct":.., "mins_pct":..}} }
      }
    """
    runs = query("SELECT * FROM runs ORDER BY date ASC")
    all_runs = query("SELECT max_hr FROM runs WHERE max_hr IS NOT NULL")
    hrmax = estimate_hrmax(all_runs) or 190
    mp_sec = _estimate_mp_sec()

    cats = ["easy", "tempo", "interval", "long", "other"]

    def _new_bucket():
        return {c: {"km": 0.0, "mins": 0.0} for c in cats}

    def _map_cat(run):
        """classify_run 的 5 類再對應到前端 4+1 類。"""
        cat, _d = classify_run(run, mp_sec, hrmax)
        if cat == "easy":
            return "easy"
        if cat in ("aerobic_long", "quality_long"):
            return "long"
        if cat == "quality":
            return "tempo" if _is_tempo_run(run) else "interval"
        return "other"

    from collections import OrderedDict
    weeks = OrderedDict()   # key: (iso_year, iso_week) -> bucket + meta
    months = OrderedDict()  # key: (year, month)        -> bucket + meta
    overall = _new_bucket()
    scatter = []            # 配速-心率散點：{pace_sec, hr, cat, date}

    for r in runs:
        try:
            dt = datetime.date.fromisoformat(r["date"])
        except (TypeError, ValueError):
            continue
        km = r.get("distance_km") or 0
        mins = r.get("duration_mins") or 0
        cat = _map_cat(r)

        # 配速-心率散點（需同時有配速與心率）
        _ps = _pace_str_to_sec(r.get("avg_pace"))
        _hr = r.get("avg_hr")
        if _ps and _hr:
            scatter.append({"pace_sec": _ps, "hr": round(_hr),
                            "cat": cat, "date": r.get("date", "")})

        # 週聚合
        iso_y, iso_w, _ = dt.isocalendar()
        wkey = (iso_y, iso_w)
        if wkey not in weeks:
            monday = datetime.date.fromisocalendar(iso_y, iso_w, 1)
            weeks[wkey] = {"meta": {"week": f"{iso_y}-W{iso_w:02d}",
                                    "start": monday.isoformat()},
                           "cats": _new_bucket()}
        weeks[wkey]["cats"][cat]["km"] += km
        weeks[wkey]["cats"][cat]["mins"] += mins

        # 月聚合
        mkey = (dt.year, dt.month)
        if mkey not in months:
            months[mkey] = {"meta": {"month": f"{dt.year}-{dt.month:02d}",
                                     "start": datetime.date(dt.year, dt.month, 1).isoformat()},
                            "cats": _new_bucket()}
        months[mkey]["cats"][cat]["km"] += km
        months[mkey]["cats"][cat]["mins"] += mins

        # 全期間聚合
        overall[cat]["km"] += km
        overall[cat]["mins"] += mins

    def _finalize(bucket):
        tot_km = sum(bucket[c]["km"] for c in cats)
        tot_mins = sum(bucket[c]["mins"] for c in cats)
        out = {}
        for c in cats:
            km = bucket[c]["km"]
            mins = bucket[c]["mins"]
            out[c] = {
                "km": round(km, 2),
                "mins": round(mins, 1),
                "km_pct": round(km / tot_km * 100, 1) if tot_km else 0.0,
                "mins_pct": round(mins / tot_mins * 100, 1) if tot_mins else 0.0,
            }
        return out, round(tot_km, 2), round(tot_mins, 1)

    weeks_out = []
    for key, wk in weeks.items():
        cats_out, tot_km, tot_mins = _finalize(wk["cats"])
        weeks_out.append({**wk["meta"], "total_km": tot_km,
                          "total_mins": tot_mins, "cats": cats_out})

    months_out = []
    for key, mo in months.items():
        cats_out, tot_km, tot_mins = _finalize(mo["cats"])
        months_out.append({**mo["meta"], "total_km": tot_km,
                           "total_mins": tot_mins, "cats": cats_out})

    overall_cats, overall_km, overall_mins = _finalize(overall)
    return json_resp({
        "mp_sec": round(mp_sec, 1),
        "hrmax": hrmax,
        "categories": cats,
        "weeks": weeks_out,
        "months": months_out,
        "overall": {"total_km": overall_km, "total_mins": overall_mins,
                    "cats": overall_cats},
        "scatter": scatter,
    })

@app.route("/api/ai-plan")
def get_ai_plan():
    if not os.path.exists(AI_PLAN_FILE):
        return json_resp({"content": None, "generated_at": None})
    with open(AI_PLAN_FILE, encoding="utf-8") as f:
        return json_resp(json.load(f))


def _save_plan(plan):
    """寫入最新課表 ai_plan.json，並 append 到歷史 ai_plan_history.json。
    plan 需含 generated_at / coach / content / schedule。
    會補上唯一 id（以 generated_at + 流水號），歷史最多保留 50 筆。"""
    # 最新一份
    with open(AI_PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    # 歷史
    history = []
    if os.path.exists(AI_PLAN_HISTORY_FILE):
        try:
            with open(AI_PLAN_HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    entry = dict(plan)
    # id：用時間戳避免碰撞（同分鐘多次生成時加流水）
    base_id = plan.get("generated_at", "").replace(" ", "_").replace(":", "")
    existing_ids = {h.get("id") for h in history}
    pid, n = base_id, 1
    while pid in existing_ids:
        pid = f"{base_id}-{n}"
        n += 1
    entry["id"] = pid
    history.append(entry)
    history = history[-50:]  # 最多保留 50 筆
    with open(AI_PLAN_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


@app.route("/api/ai-plan/history")
def get_ai_plan_history():
    """回傳歷史課表的精簡清單（新到舊）：id / generated_at / coach。"""
    if not os.path.exists(AI_PLAN_HISTORY_FILE):
        return json_resp([])
    try:
        with open(AI_PLAN_HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return json_resp([])
    meta = [{"id": h.get("id"), "generated_at": h.get("generated_at"),
             "coach": h.get("coach")} for h in history]
    meta.reverse()  # 新到舊
    return json_resp(meta)


@app.route("/api/ai-plan/history/<plan_id>")
def get_ai_plan_history_item(plan_id):
    """回傳某一筆歷史課表的完整內容。"""
    if not os.path.exists(AI_PLAN_HISTORY_FILE):
        return app.response_class(status=404)
    try:
        with open(AI_PLAN_HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return app.response_class(status=404)
    for h in history:
        if h.get("id") == plan_id:
            return json_resp(h)
    return app.response_class(status=404)

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
    from flask import request as flask_req
    _cfg = flask_req.get_json(silent=True) or {}
    lang = _cfg.get("lang", "zh")

    def job(log, emit_event):
        import uuid
        from dotenv import load_dotenv
        import garth
        from garminconnect import Garmin, GarminConnectAuthenticationError

        # frozen exe: .env 在 exe 同目錄；開發: 專案根目錄
        exe_dir = os.path.dirname(sys.executable) if _IS_FROZEN else os.path.join(_HERE, '..')
        env_path = os.path.join(exe_dir, '.env')
        # 診斷：載入 .env「之前」是否已有環境變數（區分來源）
        _email_before = os.getenv("GARMIN_EMAIL")
        load_dotenv(env_path, override=True)
        EMAIL = os.getenv("GARMIN_EMAIL")
        PASSWORD = os.getenv("GARMIN_PASSWORD")
        log(f"📁 DB: {DB_FILE}")
        log(f"🧭 frozen={_IS_FROZEN} exe_dir={exe_dir}")
        log(f"📄 .env: {env_path} ({'found' if os.path.exists(env_path) else 'NOT FOUND'})")
        log(f"🔎 Email 來源：{'既有環境變數' if _email_before else ('.env 檔' if EMAIL else '無')}")
        log(f"👤 Email: {EMAIL or 'NOT SET'}")

        if not EMAIL or not PASSWORD:
            log(tr(lang,
                   "❌ 請確認 .env 檔案放在 exe 同目錄，並設定 GARMIN_EMAIL 與 GARMIN_PASSWORD",
                   "❌ Please place .env next to the exe and set GARMIN_EMAIL and GARMIN_PASSWORD"))
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
            log(tr(lang, "🔐 需要 MFA 驗證碼，請在網頁輸入",
                   "🔐 MFA code required — please enter it on the web page"))
            emit_event("mfa", {"session_id": session_id})
            try:
                # Block up to 5 minutes for the user to submit the code.
                code = wait_q.get(timeout=300)
            except queue.Empty:
                _clear_mfa_waiter(session_id)
                raise GarminConnectAuthenticationError(
                    tr(lang, "MFA 驗證逾時（5 分鐘未輸入）",
                       "MFA verification timed out (no code within 5 minutes)"))
            log(tr(lang, "🔑 已收到 MFA 驗證碼，繼續登入...",
                   "🔑 MFA code received, continuing login..."))
            return code

        def full_login():
            """Username/password login (handles MFA) then persist tokens."""
            garth.login(EMAIL, PASSWORD, prompt_mfa=prompt_mfa)
            os.makedirs(TOKEN_DIR, exist_ok=True)
            garth.save(TOKEN_DIR)
            api = _make_api()
            api.login(TOKEN_DIR)
            return api

        log(tr(lang, "🔄 正在嘗試登入 Garmin Connect...", "🔄 Logging in to Garmin Connect..."))
        api = None
        if os.path.isdir(TOKEN_DIR):
            try:
                api = _make_api()
                api.login(TOKEN_DIR)
                log(tr(lang, "✅ 使用快取 Token 登入成功！", "✅ Logged in with cached token!"))
            except (GarminConnectAuthenticationError, AssertionError, KeyError,
                    ValueError, TypeError) as e:
                # Expired/invalid token surfaces in several ways depending on
                # garth version (AssertionError from an empty profile body,
                # KeyError on missing fields, etc). Treat all as "re-login".
                log(tr(lang,
                       f"🔑 快取 Token 無效或已過期（{type(e).__name__}），改用帳號密碼重新登入...",
                       f"🔑 Cached token invalid/expired ({type(e).__name__}), re-logging in with credentials..."))
                api = None

        if api is None:
            api = full_login()
            log(tr(lang, "✅ 帳密登入成功！", "✅ Logged in with credentials!"))

        # inline sync logic (compatible with frozen exe)
        from datetime import datetime, timedelta

        def _parse_pace(speed_ms):
            if speed_ms and speed_ms > 0:
                p = 16.6667 / speed_ms
                return f"{int(p)}:{int((p - int(p)) * 60):02d}"
            return "N/A"

        conn = sqlite3.connect(DB_FILE, timeout=10)
        ensure_schema(conn)
        existing = {r[0] for r in conn.execute("SELECT activity_id FROM runs").fetchall()}
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        end_date = datetime.now().strftime("%Y-%m-%d")
        log(tr(lang, f"📥 撈取 {start_date} ~ {end_date} 的跑步紀錄...",
               f"📥 Fetching running records from {start_date} to {end_date}..."))
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
                log(tr(lang, f"  ⚠️ 圈數資料失敗: {e}", f"  ⚠️ Failed to fetch lap data: {e}"))
            new_count += 1
        conn.commit()
        conn.close()
        log(tr(lang, f"🎉 新增 {new_count} 筆，同步完成",
               f"🎉 Added {new_count} new records, sync complete"))

    return run_job(job)

@app.route("/api/analyze", methods=["POST"])
def analyze():
    from flask import request as flask_req
    cfg = flask_req.get_json(silent=True) or {}

    def job(log, emit_event=None):
        from dotenv import load_dotenv
        import json as _json, datetime

        lang = cfg.get("lang", "zh")
        exe_dir = os.path.dirname(sys.executable) if _IS_FROZEN else os.path.join(_HERE, '..')
        load_dotenv(os.path.join(exe_dir, '.env'), override=True)
        API_KEY = os.getenv("OPENROUTER_API_KEY")
        if not API_KEY:
            log(tr(lang, "❌ 請在 .env 設定 OPENROUTER_API_KEY",
                   "❌ Please set OPENROUTER_API_KEY in .env")); return

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
            time_desc = tr(lang, f"最近 {lookback_months} 個月 (自 {start_date} 起)",
                           f"last {lookback_months} months (since {start_date})")
        else:
            runs = query("SELECT * FROM runs ORDER BY date DESC")
            time_desc = tr(lang, "全部歷史紀錄", "all history")

        if not runs:
            log(tr(lang, f"❌ {time_desc} 內無跑步紀錄！",
                   f"❌ No running records within {time_desc}!")); return
        log(tr(lang, f"✅ 載入 {len(runs)} 筆跑步紀錄 ({time_desc})，呼叫 AI 中...",
               f"✅ Loaded {len(runs)} runs ({time_desc}), calling AI..."))

        # ── AI 呼叫基礎設施（供 auto 選教練與主排課共用）────────────
        # 依序嘗試的模型：主要免費模型失敗時 fallback 到其他免費模型
        # 可用環境變數 OPENROUTER_MODELS（逗號分隔）覆寫
        # AI 呼叫（重試 + 多模型 fallback）已抽出至 ai_client.py；
        # 這裡用薄包裝綁定本 route 的 API_KEY / log / lang。
        _models = ai_client.get_models()

        def run_with_fallback(prompt_text):
            return ai_client.run_with_fallback(
                prompt_text, API_KEY, models=_models, log=log, tr=tr, lang=lang)

        # ── Auto：不套固定流派，依「跑者既有訓練框架」個人化規劃 ──────
        if coach == "auto":
            log(tr(lang, "🤖 Auto 模式：分析你的訓練框架，進行個人化規劃中...",
                   "🤖 Auto mode: analyzing your training framework for a personalized plan..."))
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
                log(tr(lang, f"🔁 間歇週期：{interval_prog['summary']}",
                       f"🔁 Interval cycle: {interval_prog['summary_en']}"))

            _na = tr(lang, "資料不足", "insufficient data")
            def _rng(t):
                return f"{_sec_to_pace_str(t[0])}~{_sec_to_pace_str(t[1])}" if t else _na
            def _hr(t):
                return f"{t[0]}~{t[1]} bpm" if t else _na

            # 心率：以 %HRmax 理論區間為主幹（遞增、寬度合理），easy 用實測校準
            hz = hr_zones_by_pct(hrmax, easy_hr_measured=fw["easy_hr"]) or {}
            def _hz(k):
                return _hr(hz.get(k)) if hz.get(k) else _na

            log(tr(lang,
                   f"📊 推估 HRmax≈{hrmax}；目標 MP≈{_sec_to_pace_str(mp_sec)}/km；"
                   f"慣用 easy≈{fw['easy_dist_med'] or '?'}km、long≈{fw['long_dist_med'] or '?'}km、"
                   f"週里程≈{fw['week_km_med']:.0f}km、質量課≈{fw['quality_per_week']:.1f}堂/週",
                   f"📊 Est. HRmax≈{hrmax}; goal MP≈{_sec_to_pace_str(mp_sec)}/km; "
                   f"typical easy≈{fw['easy_dist_med'] or '?'}km, long≈{fw['long_dist_med'] or '?'}km, "
                   f"weekly≈{fw['week_km_med']:.0f}km, quality≈{fw['quality_per_week']:.1f}/wk"))

            mp_s = _sec_to_pace_str(mp_sec)
            framework_block = tr(lang, f"""【這位跑者既有的訓練框架（由歷史數據加權分析得出，僅供你參考，不必逐字照抄）】
* 推估 HRmax：{hrmax} bpm（取自歷史 max_hr 高位穩健值，請以此為心率計算基準）
* 慣用輕鬆跑距離：中位 {fw['easy_dist_med'] or '?'} km，範圍 {fw['easy_dist_range']}
* 慣用長跑距離：中位 {fw['long_dist_med'] or '?'} km，範圍 {fw['long_dist_range']}
* 慣用節奏跑距離：中位 {fw['tempo_dist_med'] or '?'} km，最大 {fw['tempo_dist_max'] or '?'} km
* 常態週里程：中位約 {fw['week_km_med']:.0f} km
* 質量負荷頻率：約每週 {fw['quality_per_week']:.1f} 堂（含間歇、節奏跑與質量長跑）

【參考配速與心率區間（作為校準錨點，你可依跑者狀態合理微調並說明理由）】
* 輕鬆跑：配速約 {_rng(fw['easy_pace_range'])} /km（實測）｜心率 {_hz('easy')}
* 有氧長跑：配速約 {_rng(fw['long_pace_range'])} /km（實測）｜心率 {_hz('long')}
* 馬拉松配速跑(M)：{mp_s} /km｜心率 {_hz('M')}
* 節奏跑(T)：{_rng(anchors['T'])} /km｜心率 {_hz('T')}
* 長間歇(>=1600m)：{_rng(anchors['long_interval'])} /km｜心率 {_hz('long_interval')}
* 短間歇(<1600m)：{_rng(anchors['short_interval'])} /km｜心率 {_hz('short_interval')}
（心率區間以 %HRmax 生理標準為主幹、並用跑者實測校準；已由慢到快遞增。間歇課的「平均」心率因含恢復段可能偏低，上表為該強度應對應的目標心率。）""",
            f"""[The runner's existing training framework (from weighted analysis of history; for your reference, no need to copy verbatim)]
* Est. HRmax: {hrmax} bpm (robust high value from historical max_hr; use as the HR basis)
* Typical Easy distance: median {fw['easy_dist_med'] or '?'} km, range {fw['easy_dist_range']}
* Typical Long distance: median {fw['long_dist_med'] or '?'} km, range {fw['long_dist_range']}
* Typical Tempo distance: median {fw['tempo_dist_med'] or '?'} km, max {fw['tempo_dist_max'] or '?'} km
* Normal weekly mileage: median ~{fw['week_km_med']:.0f} km
* Quality load: ~{fw['quality_per_week']:.1f} sessions/week (incl. intervals, tempo and quality long runs)

[Reference pace & HR zones (calibration anchors; you may fine-tune with reason)]
* Easy: ~{_rng(fw['easy_pace_range'])} /km (measured) | HR {_hz('easy')}
* Aerobic Long: ~{_rng(fw['long_pace_range'])} /km (measured) | HR {_hz('long')}
* Marathon Pace (M): {mp_s} /km | HR {_hz('M')}
* Tempo (T): {_rng(anchors['T'])} /km | HR {_hz('T')}
* Long Interval (>=1600m): {_rng(anchors['long_interval'])} /km | HR {_hz('long_interval')}
* Short Interval (<1600m): {_rng(anchors['short_interval'])} /km | HR {_hz('short_interval')}
(HR zones use %HRmax physiology as the backbone, calibrated by measured data; already increasing slow->fast. Interval AVERAGE HR may be lower due to recovery segments; the table shows the target HR for that intensity.)""")

            structure_rules = tr(lang, f"""【硬底線（這幾條務必遵守，其餘請發揮你的教練專業判斷）】
1. 有氧課（輕鬆跑/有氧長跑）配速「不得快於」馬拉松配速（MP {mp_s}/km）——輕鬆跑本就應比 MP 慢約 50~90 秒/km，這是正確設計，請「勿」把它當缺點或建議加速。
2. 間歇課「不得倒退回短間歇」（見下方間歇進程說明）；若在減量期則見減量守則。
3. 減量期「不得增加」訓練量（見下方減量守則，若有）。
4. 各區配速/心率請落在上方「參考區間」附近；心率須由慢到快遞增，且課表備註與診斷所用心率彼此一致（數值相近即可，不必逐字相同）。

【軟性建議（供參考，你可依專業與跑者狀態調整，但請說明理由）】
* 盡量尊重跑者既有訓練量：輕鬆跑距離貼近慣用值、長跑沿用慣用範圍、週里程與常態相當。
* 節奏跑距離可參考其慣用值（約 {fw['tempo_dist_med'] or '?'} km），配速維持在節奏跑區間。
* 質量課之間盡量間隔一天輕鬆跑或休息，避免連續兩天高強度。
* 質量長跑（快長跑）當天視為一堂質量課計入負荷。
* 診斷、編排與強度分配請以你的教練專業自由發揮，提出有洞察的觀察與個人化建議。""",
            f"""[Hard limits (must follow; use your coaching judgement for the rest)]
1. Aerobic runs (Easy/Aerobic Long) must NOT be faster than Marathon Pace (MP {mp_s}/km) — easy runs should be ~50-90s/km slower than MP by design; do NOT treat this as a weakness or suggest speeding up.
2. Intervals must NOT regress to short intervals (see interval-progression note below); in taper, see taper rules.
3. During taper, do NOT increase training volume (see taper rules below, if any).
4. Keep each zone's pace/HR near the reference ranges above; HR must increase slow->fast, and the HR used in the schedule notes must be consistent with the diagnosis (close values are fine).

[Soft guidance (reference; adjust with your judgement and state reasons)]
* Respect the runner's existing volume where possible: easy distance near typical, long within typical range, weekly mileage near normal.
* Tempo distance can reference the typical value (~{fw['tempo_dist_med'] or '?'} km), keeping pace in the tempo range.
* Try to separate quality sessions by an easy/rest day; avoid two hard days in a row.
* A quality long run (fast long run) counts as one quality session that day.
* Feel free to use your coaching expertise for diagnosis, scheduling and intensity distribution, offering insightful observations and personalized advice.""")

            race_line = ""
            if race_type in race_type_names:
                rtname = race_type_names[race_type][0 if lang != "en" else 1]
                if lang == "en":
                    race_line = f"\n* Target race: {rtname}"
                    if race_date:
                        race_line += f", on {race_date}"
                        if weeks_to_race is not None:
                            race_line += f" (~{weeks_to_race:.1f} weeks away)"
                    if race_goal:
                        race_line += f"; goal finish {race_goal} (MP≈{_sec_to_pace_str(mp_sec)}/km)"
                else:
                    race_line = f"\n* 目標賽事：{rtname}"
                    if race_date:
                        race_line += f"，賽事日期 {race_date}"
                        if weeks_to_race is not None:
                            race_line += f"（距今約 {weeks_to_race:.1f} 週）"
                    if race_goal:
                        race_line += f"；目標完賽 {race_goal}（MP≈{_sec_to_pace_str(mp_sec)}/km）"
            note_block = (tr(lang, f"\n【跑者補充訊息】\n{note}", f"\n[Runner's notes]\n{note}")
                          if note else "")
            if lang == "en":
                day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                rest_s = ", ".join(day_names[d] for d in rest_days) if rest_days else "none"
                lsd_s  = ", ".join(day_names[d] for d in lsd_days)  if lsd_days  else "none"
            else:
                day_names = ["週日", "週一", "週二", "週三", "週四", "週五", "週六"]
                rest_s = "、".join(day_names[d] for d in rest_days) if rest_days else "無"
                lsd_s  = "、".join(day_names[d] for d in lsd_days)  if lsd_days  else "無"

            interval_block = ""
            if interval_prog:
                rmax = interval_prog["recent_max"]
                stage = interval_prog["stage"]
                if lang == "en":
                    stage_en = "long intervals" if stage == "long" else "short intervals"
                    interval_block = f"""
[Interval cycle progression (important: continue, don't regress)]
* {interval_prog['summary_en']}
* The runner is currently in the "{stage_en}" phase; recent max rep distance is {rmax}m.
* Unless the runner explicitly asks to reduce/shorten, next week's interval rep distance must be no shorter than {rmax}m, and must not regress to short intervals (<1600m); maintain or progress toward more race-specific work."""
                    if stage == "long":
                        interval_block += (f" You may use long intervals (rep >= {rmax}m, e.g. keep {rmax}m with more reps, "
                                           "progress to longer reps, or shorten recovery), or shift toward marathon-pace work as the race nears.")
                    else:
                        interval_block += " You may continue short intervals and gradually lengthen rep distance toward long intervals."
                    if note:
                        interval_block += " (Note: if the runner's notes specify interval preferences, follow those.)"
                else:
                    stage_zh = "長間歇" if stage == "long" else "短間歇"
                    interval_block = f"""
【間歇訓練週期進程（重要，請延續而非倒退）】
* {interval_prog['summary']}
* 這位跑者目前的間歇訓練「已進入{stage_zh}階段」，近期已達到的最大單趟距離為 {rmax}m。
* 「除非跑者在補充訊息中明確要求降低/縮短」，否則下週間歇課的「單趟距離不得低於 {rmax}m」，且不得倒退回短間歇(<1600m)；應維持或往更專項方向推進。"""
                    if stage == "long":
                        interval_block += (f"可安排長間歇(單趟>={rmax}m，例如維持 {rmax}m 並增加組數、"
                                           "漸進至更長單趟、或縮短恢復)，或依賽事臨近程度轉為馬拉松配速跑的專項課。")
                    else:
                        interval_block += "可延續短間歇並視進度逐步拉長單趟距離，往長間歇推進。"
                    if note:
                        interval_block += "（注意：若上方跑者補充訊息有特別指示間歇安排，以其要求為準。）"

            # ── 賽前減量期偵測（距賽 <=2 週進入 taper）──────────────
            taper_block = ""
            is_taper = weeks_to_race is not None and weeks_to_race <= 2
            if is_taper:
                log(tr(lang, f"🏁 賽前減量期（距賽約 {weeks_to_race:.1f} 週）：改為減量守則",
                       f"🏁 Race taper ({weeks_to_race:.1f} weeks out): applying taper rules"))
                taper_block = tr(lang, f"""
【★賽前減量期（Taper）守則——目前距賽約 {weeks_to_race:.1f} 週，「本區優先於上方防倒退規則」】
* 現已進入賽前減量期，目標是「消除累積疲勞、讓身體超補償」，而非再增加訓練負荷。
* 「減量」：本週總里程應「明顯低於」常態週里程（距賽第2週約減 20~30%、最後1週約減 40~50%）；質量課的「總量」要縮減（堂數與每堂的反覆組數/距離都減少）。
* 「維持強度」：質量課的「配速」與「單趟距離」仍維持在原本水準（例如長間歇仍用 2000m 單趟、節奏跑配速不放慢），只是「組數/總距離變少」。「不要」為了減量而放慢配速或縮短單趟——那會讓比賽日腿感變鈍。
* 因此，上方「間歇單趟不得低於近期最大」「節奏跑距離不得低於慣用」等「防倒退規則，在減量期讓路」：允許減少組數/總距離，但仍維持單趟距離與配速。
* Easy/長跑配速維持不變，只是距離縮短、總量下降。""",
                f"""
[★ Race taper rules — ~{weeks_to_race:.1f} weeks to race; THIS SECTION OVERRIDES the no-regression rules above]
* You are in the race taper. The goal is to shed accumulated fatigue and allow supercompensation, NOT to add load.
* REDUCE VOLUME: weekly mileage should be clearly below normal (2nd week out ~20-30% less; final week ~40-50% less); cut the TOTAL of quality work (fewer sessions, fewer reps/shorter distance per session).
* MAINTAIN INTENSITY: keep the PACE and REP DISTANCE of quality work (e.g. long intervals still 2000m, tempo pace not slowed) — only reduce reps/total distance. Do NOT slow pace or shorten rep distance for the taper; that dulls race-day legs.
* Therefore the "interval rep not below recent max" / "tempo not below typical" no-regression rules YIELD during taper: fewer reps/less total distance is fine, but keep rep distance and pace.
* Easy/long paces unchanged; only distance and total volume drop.""")

            terms_block = TERMINOLOGY_EN if lang == "en" else TERMINOLOGY_ZH
            data_header = tr(lang, f"【跑步歷史數據 (JSON - {time_desc})】",
                             f"[Running history (JSON - {time_desc})]")

            if lang == "en":
                intro = ("IMPORTANT: Respond ENTIRELY in English. The data below may contain Chinese "
                         "text (e.g. activity names); ignore its language and write your whole answer in English only.\n\n"
                         "You are an elite personalized marathon coach. You do NOT force a fixed methodology "
                         "(no forcing Daniels/Hansons/Lydiard); instead you respect and build on the runner's "
                         "existing training framework, only calibrating paces, intensity distribution and race-specific periodization for the goal race.")
                goal_hdr = "[Runner's goal]"
                fixed_line = f"* Fixed rest days: {rest_s}; LSD long-run days: {lsd_s}"
                tasks = """[Tasks — respond entirely in English]
1. Framework diagnosis: use your coaching expertise to interpret the runner's data (pace/HR/volume/trend), offering insightful observations, strengths and areas to improve. The framework and reference ranges above are for reference; you may add your own judgement.
2. Pace & HR zones: base them on the reference ranges above, fine-tuning with reason; ensure HR increases slow->fast.
3. Next week's plan: schedule with your expertise, respecting the runner's existing volume and rhythm where possible; apply appropriate intensity distribution and periodization for the goal race. If in the race taper (see taper rules above), the taper rules take priority (reduce volume but keep pace and rep intensity). Explain any change to existing volume.
4. Before finalizing, confirm the hard limits hold: aerobic not faster than MP, intervals not regressed to short intervals, no volume increase during taper, HR increasing slow->fast and consistent. Also verify: (a) the sum of per-day distances equals the stated weekly total (numbers must match); (b) warm-up/cool-down pace is easy-run pace (slower than the main set), not the same as the main set. Fix any violations. Leave other details to your judgement."""
                auto_prompt = f"""{intro}

{goal_hdr}{race_line}
{fixed_line}{note_block}

{framework_block}

{structure_rules}
{interval_block}
{taper_block}

{terms_block}

{data_header}
{_json.dumps(runs, ensure_ascii=False, indent=2)}

{tasks}"""
            else:
                auto_prompt = f"""你是一位頂尖的個人化馬拉松教練。你「不套用任何固定訓練流派(不強行套 Daniels/Hansons/Lydiard)」，而是「尊重並沿用這位跑者既有的訓練框架」，只針對其目標賽事做配速校準、強度分配與賽前週期化調整。

【跑者目標】{race_line}
* 固定休息日：{rest_s}；LSD 長跑日：{lsd_s}{note_block}

{framework_block}

{structure_rules}
{interval_block}
{taper_block}

{terms_block}

{data_header}
{_json.dumps(runs, ensure_ascii=False, indent=2)}

【請執行以下任務，全程使用繁體中文】
1. 訓練框架診斷：以你的教練專業「自由解讀」這位跑者的數據（配速/心率/量/趨勢），提出有洞察的觀察、優點與可改進處。上方框架與參考區間供你參考，你可提出自己的判斷。
2. 配速與心率區間：以上方「參考區間」為基礎給出各區配速與心率，可依跑者狀態合理微調並說明；確保心率由慢到快遞增。
3. 下週課表：發揮你的專業編排，盡量尊重跑者既有訓練量與節奏；針對目標賽事做適當的強度分配與週期化。若已進入賽前減量期（見上方 Taper 守則），以減量守則為優先（減量但維持配速與單趟強度）。任何調整既有量之處請說明理由。
4. 定稿前，請確認有守住「硬底線」：有氧課不快於 MP、間歇未倒退回短間歇、減量期未增量、心率由慢到快遞增且前後一致。另請驗算：(a)「課表各天距離的加總」必須等於你所標示的「週里程總計」，數字務必一致；(b) 熱身/冷卻跑的配速應為「輕鬆跑配速」（比主課慢），不得與主課相同配速（否則失去熱身/冷卻意義）。若有違反請修正。其餘細節由你的專業判斷決定。"""

            auto_prompt += (prompts.SCHEDULE_JSON_INSTRUCTION_EN if lang == "en"
                            else prompts.SCHEDULE_JSON_INSTRUCTION_ZH)
            content = run_with_fallback(auto_prompt)
            if content is None:
                return
            schedule = prompts.extract_schedule(content)      # 清理前先解析 JSON
            content = prompts.strip_schedule_json(content)    # 移除原始 json block
            content = content.replace(r"\&", "&").replace(r"\~", "~").replace(r"\text{", "").replace("}", "")
            content = normalize_terms(content)
            _save_plan({"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "coach": "auto", "content": content, "schedule": schedule})
            log(tr(lang, "💾 課表已儲存至 ai_plan.json", "💾 Plan saved to ai_plan.json"))
            log(tr(lang, "✅ AI 個人化分析完成，請重新整理頁面查看課表。",
                   "✅ Personalized AI analysis complete. Refresh the page to view the plan."))
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
            coach_rules = prompts.COACH_RULES_EN.get(coach, "")
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
3. Before finalizing, VERIFY: (a) paces obey the STRICT ordering (e.g. Easy/Long slower than MP; for Hansons the Tempo run equals MP and the Long run is not faster than Easy); (b) the sum of per-day distances equals the weekly total you state (numbers must match); (c) warm-up/cool-down paces are easy-run pace (slower than the main set, not the same). Fix any violations.
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
            coach_rules = prompts.COACH_RULES_ZH.get(coach, "")
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
3. 定稿前請「自我檢查」：(a) 各配速是否符合上述定義的嚴格由慢到快順序（例如：輕鬆跑/長跑必須慢於 MP；Hansons 的 Tempo 課＝MP、長跑不得快於輕鬆跑）；(b) 課表各天距離加總是否等於你標示的週里程總計（數字須一致）；(c) 熱身/冷卻配速是否為輕鬆跑配速（比主課慢，不得與主課同配速）。若有違反請修正後再輸出。
4. 編排下週動態訓練課表。
"""
        prompt += (prompts.SCHEDULE_JSON_INSTRUCTION_EN if lang == "en"
                   else prompts.SCHEDULE_JSON_INSTRUCTION_ZH)
        content = run_with_fallback(prompt)
        if content is None:
            return
        schedule = prompts.extract_schedule(content)      # 清理前先解析 JSON
        content = prompts.strip_schedule_json(content)    # 移除原始 json block
        # 清理 AI 可能輸出的 LaTeX 轉義字元，避免 MathJax 渲染錯誤
        content = content.replace(r"\&", "&").replace(r"\~", "~").replace(r"\text{", "").replace("}", "")
        content = normalize_terms(content)
        
        _save_plan({"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "coach": coach, "content": content, "schedule": schedule})
        log(tr(lang, "💾 課表已儲存至 ai_plan.json", "💾 Plan saved to ai_plan.json"))
        log(tr(lang, "✅ AI 分析完成，請重新整理頁面查看課表。",
               "✅ AI analysis complete. Refresh the page to view the plan."))

    return run_job(job)

if __name__ == "__main__":
    app.run(debug=False, port=5000)
