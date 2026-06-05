#!/usr/bin/env python3
import os
import json
import sqlite3
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
DB_FILE = "garmin_running_history.db"

def load_runs_from_db():
    if not os.path.exists(DB_FILE):
        return None
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM runs ORDER BY date DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def ask_ai_coach():
    if not API_KEY:
        print("❌ 錯誤: 請在 .env 中設定 OPENROUTER_API_KEY")
        return

    # 1. 從 SQLite 讀取跑步數據
    running_history = load_runs_from_db()
    if running_history is None:
        print(f"❌ 找不到資料庫 {DB_FILE}，請先執行 garmin_sync.py！")
        return
    if not running_history:
        print("❌ 資料庫中尚無跑步紀錄，請先執行 garmin_sync.py！")
        return
    print(f"✅ 從資料庫載入 {len(running_history)} 筆跑步紀錄")

    # 2. 撰寫教練 Prompt
    prompt = f"""
    你是一位精通「丹尼爾博士科學化跑步方程式」與現代穿戴裝置數據分析的國家級田徑教練。
    
    【使用者當前目標】
    * 目標：今年下半年挑戰全程馬拉松突破 Sub 2:54（目標配跨約為 4:04/km）。
    * 訓練限制：一週可練跑 5 天（週一、週五固定休息），週日可進行長距離（Long Run）。
    
    【跑步歷史數據 (JSON 格式)】
    {json.dumps(running_history, ensure_ascii=False, indent=2)}
    
    【請教練執行以下任務】
    1. 體能與疲勞診斷：分析我最近幾次跑步的「平均心率與配速關係」。我的有氧基礎是否紮實？
    2. 計算當前配速區間：根據我近期的表現，列出我下週應該執行的 E（輕鬆跑）、M（馬拉松）、T（乳酸門檻）、I（間歇）配速。
    3. 編排下週動態訓練課表：為我量身打造下週 5 天的具體課表。包含距離/時間、目標配速與 RPE 強度。
    """

    print("🤖 AI 教練正在審閱你的 Garmin 數據並規劃課表中 (透過 OpenRouter 免費通道)...")
    
    # 3. OpenRouter 標準 API 端點
    url = "https://openrouter.ai/api/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 4. 設定 Payload，使用 OpenRouter 提供的免費版 Gemini 2.0 Thinking 模型
    payload = {
        "model": "google/gemma-4-31b-it:free",
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ]
    }

    # 5. 發送請求
    try:
        response = requests.post(url, headers=headers, json=payload)
        response_json = response.json()

        if response.status_code == 200:
            # 解析 OpenRouter 標準 OpenAI-like 回傳格式
            ai_response = response_json['choices'][0]['message']['content']
            
            print("\n================== 📋 AI 教練的即時回饋 ==================\n")
            print(ai_response)
            print("\n==========================================================")
            
            # 存成 JSON 供 dashboard 讀取
            with open("ai_plan.json", "w", encoding="utf-8") as f:
                json.dump({
                    "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "content": ai_response
                }, f, ensure_ascii=False, indent=2)
            print("💾 課表已儲存至 ai_plan.json")
            
        else:
            print(f"❌ 呼叫 AI 失敗，狀態碼: {response.status_code}")
            print(f"錯誤訊息: {json.dumps(response_json, indent=2, ensure_ascii=False)}")

    except Exception as e:
        print(f"❌ 網路連線或解析失敗: {e}")

if __name__ == "__main__":
    ask_ai_coach()
