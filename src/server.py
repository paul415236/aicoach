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
    """Run fn in a thread, stream log lines as SSE."""
    import traceback
    q = queue.Queue()
    def worker():
        try:
            fn(lambda msg: q.put(str(msg)))
        except Exception:
            q.put("❌ " + traceback.format_exc())
        finally:
            q.put(None)  # sentinel
    threading.Thread(target=worker, daemon=True).start()
    def generate():
        while True:
            msg = q.get()
            if msg is None:
                yield "event: done\ndata: \n\n"
                break
            yield f"data: {msg}\n\n"
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

@app.route("/api/sync", methods=["POST"])
def sync():
    def job(log):
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

        log("🔄 正在嘗試登入 Garmin Connect...")
        api = None
        if os.path.isdir(TOKEN_DIR):
            try:
                api = Garmin()
                api.login(TOKEN_DIR)
                log("✅ 使用快取 Token 登入成功！")
            except GarminConnectAuthenticationError:
                log("🔑 快取 Token 已過期，使用帳號密碼重新登入...")
                api = None
        if api is None:
            garth.login(EMAIL, PASSWORD)
            os.makedirs(TOKEN_DIR, exist_ok=True)
            garth.save(TOKEN_DIR)
            api = Garmin()
            api.login(TOKEN_DIR)
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

    def job(log):
        import requests as req
        from dotenv import load_dotenv
        import json as _json, datetime

        exe_dir = os.path.dirname(sys.executable) if _IS_FROZEN else os.path.join(_HERE, '..')
        load_dotenv(os.path.join(exe_dir, '.env'))
        API_KEY = os.getenv("OPENROUTER_API_KEY")
        if not API_KEY:
            log("❌ 請在 .env 設定 OPENROUTER_API_KEY"); return

        runs = query("SELECT * FROM runs ORDER BY date DESC")
        if not runs:
            log("❌ 資料庫尚無跑步紀錄，請先同步 Garmin 數據！"); return
        log(f"✅ 載入 {len(runs)} 筆跑步紀錄，呼叫 AI 中...")

        # ── 解析設定 ──────────────────────────────────────
        coach = cfg.get("coach", "daniels")
        rest_days = cfg.get("rest_days", [1, 5])
        lsd_days  = cfg.get("lsd_days",  [0])
        note      = cfg.get("note", "").strip()

        day_names = ["週日","週一","週二","週三","週四","週五","週六"]
        rest_str = "、".join(day_names[d] for d in rest_days) if rest_days else "無"
        lsd_str  = "、".join(day_names[d] for d in lsd_days)  if lsd_days  else "無"

        coach_desc = {
            "daniels":  "Jack Daniels 科學化跑步方程式（E/M/T/I/R 配速區間）",
            "hansons":  "Hansons 馬拉松訓練法（累積疲勞、SOS 課、Never 20 miles long run）",
            "lydiard":  "Lydiard 週期化訓練（有氧基礎→山坡強化→田徑期→賽季）",
        }.get(coach, "Jack Daniels 科學化跑步方程式")

        note_section = f"\n【跑者補充訊息】\n{note}" if note else ""

        prompt = f"""
你是一位精通「{coach_desc}」的國家級馬拉松教練。

【使用者當前目標】
* 目標：今年下半年挑戰全程馬拉松突破 Sub 2:54（目標配速約為 4:04/km）。
* 固定休息日：{rest_str}（這幾天不安排任何跑步訓練）
* LSD 長跑日：{lsd_str}（這幾天安排長距離慢跑）{note_section}

【跑步歷史數據 (JSON 格式)】
{_json.dumps(runs, ensure_ascii=False, indent=2)}

【請依照 {coach_desc} 的訓練哲學，執行以下任務】
1. 體能與疲勞診斷：分析最近幾次跑步的「平均心率與配速關係」，有氧基礎是否紮實？
2. 計算當前配速區間：根據近期表現，列出下週應執行的各訓練區間配速。
3. 編排下週動態訓練課表：依照休息日（{rest_str} 不跑）與 LSD 日（{lsd_str} 安排長跑），為每天量身打造具體課表，包含距離/時間、目標配速與 RPE 強度。
"""
        resp = req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={"model": "google/gemma-4-31b-it:free",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120
        )
        if resp.status_code != 200:
            log(f"❌ AI 呼叫失敗 {resp.status_code}: {resp.text[:200]}"); return

        content = resp.json()["choices"][0]["message"]["content"]
        with open(AI_PLAN_FILE, "w", encoding="utf-8") as f:
            _json.dump({"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "coach": coach, "content": content}, f, ensure_ascii=False, indent=2)
        log("💾 課表已儲存至 ai_plan.json")
        log("✅ AI 分析完成，請重新整理頁面查看課表。")

    return run_job(job)

if __name__ == "__main__":
    app.run(debug=False, port=5000)
