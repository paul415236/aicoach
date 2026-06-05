#!/usr/bin/env python3
import os
import json
import requests
from dotenv import load_dotenv

# 載入環境變數
load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")

def ask_ai_coach(json_data_path):
    if not API_KEY:
        print("❌ 錯誤: 請在 .env 中設定 OPENROUTER_API_KEY")
        return

    # 1. 讀取本地的 Garmin 跑步數據
    try:
        with open(json_data_path, "r", encoding="utf-8") as f:
            running_history = json.load(f)
    except FileNotFoundError:
        print(f"❌ 錯誤: 找不到 {json_data_path}，請先執行同步腳本！")
        return

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
            
            # 存成 Markdown 檔案
            with open("next_week_plan.md", "w", encoding="utf-8") as f:
                f.write(ai_response)
            print("💾 課表已成功導出至 next_week_plan.md")
            
        else:
            print(f"❌ 呼叫 AI 失敗，狀態碼: {response.status_code}")
            print(f"錯誤訊息: {json.dumps(response_json, indent=2, ensure_ascii=False)}")

    except Exception as e:
        print(f"❌ 網路連線或解析失敗: {e}")

if __name__ == "__main__":
    ask_ai_coach("garmin_running_history.json")
