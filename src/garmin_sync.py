#!/usr/bin/python3
# 免責聲明：本程式使用非官方 garminconnect/garth 套件存取 Garmin Connect 個人數據。
# 僅供個人學習用途，使用者須自行承擔違反 Garmin 服務條款之風險。
# Disclaimer: This script uses unofficial third-party libraries to access Garmin Connect.
# For personal/educational use only. Use at your own risk.
import os
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

load_dotenv()
EMAIL = os.getenv("GARMIN_EMAIL")
PASSWORD = os.getenv("GARMIN_PASSWORD")

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, '..', 'data')
TOKEN_DIR = os.path.join(DATA_DIR, ".garminconnect_token")
DB_FILE = os.path.join(DATA_DIR, "garmin_running_history.db")

def init_garmin_api():
    if not EMAIL or not PASSWORD:
        raise ValueError("請在 .env 設定 GARMIN_EMAIL 與 GARMIN_PASSWORD")

    print("🔄 正在嘗試登入 Garmin Connect...")
    if os.path.isdir(TOKEN_DIR):
        try:
            api = Garmin()
            api.login(TOKEN_DIR)
            print("✅ 使用快取 Token 登入成功！")
            return api
        except GarminConnectAuthenticationError:
            print("🔑 快取 Token 已過期，使用帳號密碼重新登入...")

    try:
        import garth
        garth.login(EMAIL, PASSWORD, prompt_mfa=lambda: input("🔐 請輸入 MFA 驗證碼: "))
        os.makedirs(TOKEN_DIR, exist_ok=True)
        garth.save(TOKEN_DIR)
        api = Garmin()
        api.login(TOKEN_DIR)
        print("✅ 帳密登入成功，已更新本地 Token 快取！")
        return api
    except GarminConnectTooManyRequestsError:
        print("❌ 請求過於頻繁，請稍後再試。")
    except Exception as e:
        print(f"❌ 登入失敗: {e}")
    return None

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            activity_id INTEGER PRIMARY KEY,
            date TEXT,
            name TEXT,
            distance_km REAL,
            duration_mins REAL,
            elevation_gain_m REAL,
            avg_pace TEXT,
            avg_hr REAL,
            max_hr REAL,
            avg_cadence REAL,
            training_effect REAL,
            anaerobic_effect REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER,
            lap INTEGER,
            distance_km REAL,
            duration_mins REAL,
            avg_pace TEXT,
            avg_hr REAL,
            max_hr REAL,
            avg_cadence REAL,
            elevation_gain_m REAL,
            FOREIGN KEY (activity_id) REFERENCES runs(activity_id)
        )
    """)
    conn.commit()
    return conn

def get_existing_ids(conn):
    rows = conn.execute("SELECT activity_id FROM runs").fetchall()
    return {row[0] for row in rows}

def parse_pace(speed_ms):
    if speed_ms and speed_ms > 0:
        p = 16.6667 / speed_ms
        return f"{int(p)}:{int((p - int(p)) * 60):02d}"
    return "N/A"

def parse_running_data(activity):
    return {
        "activity_id": activity.get("activityId"),
        "date": activity.get("startTimeLocal", "")[:10],
        "name": activity.get("activityName", "跑步活動"),
        "distance_km": round(activity.get("distance", 0) / 1000, 2),
        "duration_mins": round(activity.get("duration", 0) / 60, 1),
        "elevation_gain_m": round(activity.get("elevationGain", 0), 1),
        "avg_pace": parse_pace(activity.get("averageSpeed", 0)),
        "avg_hr": activity.get("averageHR"),
        "max_hr": activity.get("maxHR"),
        "avg_cadence": activity.get("averageRunningCadenceInStepsPerMinute"),
        "training_effect": activity.get("aerobicTrainingEffect"),
        "anaerobic_effect": activity.get("anaerobicTrainingEffect"),
    }

def save_run(conn, run):
    conn.execute("""
        INSERT OR IGNORE INTO runs VALUES (
            :activity_id, :date, :name, :distance_km, :duration_mins,
            :elevation_gain_m, :avg_pace, :avg_hr, :max_hr,
            :avg_cadence, :training_effect, :anaerobic_effect
        )
    """, run)
    conn.commit()

def save_splits(conn, activity_id, splits_data):
    laps = splits_data.get("lapDTOs", [])
    for i, lap in enumerate(laps, 1):
        conn.execute("""
            INSERT INTO activity_splits
            (activity_id, lap, distance_km, duration_mins, avg_pace, avg_hr, max_hr, avg_cadence, elevation_gain_m)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            activity_id,
            i,
            round(lap.get("distance", 0) / 1000, 3),
            round(lap.get("duration", 0) / 60, 2),
            parse_pace(lap.get("averageSpeed", 0)),
            lap.get("averageHR"),
            lap.get("maxHR"),
            lap.get("averageRunCadence"),
            round(lap.get("elevationGain", 0) or 0, 1),
        ))
    conn.commit()

def fetch_and_sync(api, conn):
    start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")
    existing_ids = get_existing_ids(conn)

    print(f"📥 撈取 {start_date} ~ {end_date} 的跑步紀錄...")
    try:
        activities = api.get_activities_by_date(start_date, end_date, "running")
    except Exception as e:
        print(f"❌ 抓取失敗: {e}")
        return

    new_count = 0
    for act in activities:
        if act.get("activityType", {}).get("typeKey") != "running":
            continue
        run = parse_running_data(act)
        activity_id = run["activity_id"]
        if activity_id not in existing_ids:
            save_run(conn, run)
            # 撈逐圈資料
            try:
                splits = api.get_activity_splits(activity_id)
                save_splits(conn, activity_id, splits)
                print(f"  ✅ {run['date']} {run['name']} — {len(splits.get('lapDTOs', []))} 圈")
            except Exception as e:
                print(f"  ⚠️  {activity_id} 逐圈資料失敗: {e}")
            new_count += 1

    print(f"🎉 完成！新增 {new_count} 筆，資料庫共 {len(existing_ids) + new_count} 筆跑步紀錄。")

if __name__ == "__main__":
    garmin_api = init_garmin_api()
    if garmin_api:
        conn = init_db()
        fetch_and_sync(garmin_api, conn)
        conn.close()
        print(f"💾 資料庫: {DB_FILE}")
