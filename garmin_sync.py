#!/usr/bin/python3
import os
import json
from datetime import datetime
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# 1. 載入環境變數
load_dotenv()
EMAIL = os.getenv("GARMIN_EMAIL")
PASSWORD = os.getenv("GARMIN_PASSWORD")
TOKEN_DIR = ".garminconnect_token"

def init_garmin_api():
    """初始化 Garmin API 並處理 Token 快取登入"""
    if not EMAIL or not PASSWORD:
        raise ValueError("錯誤: 請在 .env 檔案中設定 GARMIN_EMAIL 與 GARMIN_PASSWORD")
    
    print("🔄 正在嘗試登入 Garmin Connect...")
    # 嘗試讀取本地快取的 Token 目錄
    if os.path.isdir(TOKEN_DIR):
        try:
            api = Garmin()
            api.login(TOKEN_DIR)
            print("✅ 使用快取 Token 登入成功！")
            return api
        except GarminConnectAuthenticationError:
            print("🔑 快取 Token 已過期，使用帳號密碼重新登入...")
    
    # 使用帳密登入（支援 MFA）
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
        print("❌ 錯誤: 請求過於頻繁，已被 Garmin 暫時限制，請稍後再試。")
    except Exception as e:
        print(f"❌ 登入失敗: {e}")
        return None

def parse_running_data(activity):
    """清洗原始複雜數據，只保留 AI 課表最需要的科學指標"""
    # 轉換秒速為 分:秒/公里
    avg_speed = activity.get("averageSpeed", 0) # 公尺/秒
    if avg_speed > 0:
        pace_min_per_km = 16.6667 / avg_speed
        minutes = int(pace_min_per_km)
        seconds = int((pace_min_per_km - minutes) * 60)
        pace_str = f"{minutes}:{seconds:02d}"
    else:
        pace_str = "N/A"

    return {
        "activity_id": activity.get("activityId"),
        "date": activity.get("startTimeLocal", "")[:10], # 取 YYYY-MM-DD
        "name": activity.get("activityName", "跑步活動"),
        "distance_km": round(activity.get("distance", 0) / 1000, 2),
        "duration_mins": round(activity.get("duration", 0) / 60, 1),
        "elevation_gain_m": round(activity.get("elevationGain", 0), 1),
        "avg_pace": pace_str,
        "avg_hr": activity.get("averageHR"),
        "max_hr": activity.get("maxHR"),
        "avg_cadence": activity.get("averageRunningCadenceInStepsPerMinute"),
        "training_effect": activity.get("aerobicTrainingEffect"), # 有氧訓練效果 (0.0-5.0)
        "anaerobic_effect": activity.get("anaerobicTrainingEffect") # 無氧訓練效果
    }

def fetch_history_running(api, limit=50):
    """撈取歷史活動並過濾出跑步資料"""
    print(f"📥 正在下載最近的 {limit} 筆活動紀錄...")
    try:
        activities = api.get_activities(start=0, limit=limit)
    except Exception as e:
        print(f"❌ 抓取數據失敗: {e}")
        return []

    running_history = []
    for act in activities:
        if act.get("activityType", {}).get("typeKey") == "running":
            clean_data = parse_running_data(act)
            running_history.append(clean_data)

    print(f"🎉 處理完成！在 {limit} 筆紀錄中，共篩選出 {len(running_history)} 筆跑步資料。")
    return running_history

if __name__ == "__main__":
    # 1. 初始化登入
    garmin_api = init_garmin_api()
    
    if garmin_api:
        # 2. 獲取最近 50 筆活動中的所有跑步紀錄 (可自行加大 limit 抓取更多歷史)
        running_data = fetch_history_running(garmin_api, limit=50)
        
        # 3. 儲存為本地 JSON 檔案
        output_filename = f"garmin_running_history.json"
        with open(output_filename, "w", encoding="utf-8") as f:
            json.dump(running_data, f, ensure_ascii=False, indent=2)
            
        print(f"💾 資料已成功儲存至: {output_filename}")
