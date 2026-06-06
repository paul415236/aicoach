# AI Coach — Garmin 跑步 AI 教練

> ⚠️ **免責聲明 Disclaimer**
>
> 本專案使用非官方第三方套件（`garminconnect` / `garth`）存取 Garmin Connect 個人數據，**並非 Garmin 官方授權之整合方案**。此行為可能違反 [Garmin Connect 服務條款](https://www.garmin.com/en-US/legal/terms-of-use/)第 14 條關於自動化存取之限制。
>
> - 本專案**僅供個人學習與研究用途**，不得用於任何商業目的。
> - 使用者須自行承擔使用本專案的所有法律風險，作者不對任何損失或帳號停權負責。
> - 本專案與 Garmin Ltd. 或其子公司**無任何關聯**，Garmin 相關商標均屬其各自所有人。

自動同步 Garmin Connect 跑步數據，透過 AI 分析體能狀態並生成個人化馬拉松訓練課表，並以 Web Dashboard 視覺化呈現。

## 功能

- 從 Garmin Connect 同步跑步歷史與分圈數據至本地 SQLite
- 使用 OpenRouter（Gemma 4）根據跑步數據生成下週訓練課表（E/M/T/I 配速區間）
- Flask Web 儀表板呈現跑步趨勢、分圈配速與 AI 課表

## 快速開始

### 1. 安裝依賴

```bash
bash install.sh
```

### 2. 設定環境變數

建立 `.env` 檔案：

```env
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=yourpassword
OPENROUTER_API_KEY=sk-or-...
```

> `OPENROUTER_API_KEY` 至 [https://openrouter.ai/](https://openrouter.ai/) 註冊後，在 **Keys** 頁面建立即可免費取得。

### 3. 啟動

```bash
python aicoach.py
```

自動啟動 Web 儀表板並開啟瀏覽器 `http://localhost:5000`。

在儀表板上可執行：
- **同步 Garmin 數據** — 從 Garmin Connect 拉取最近一年跑步紀錄
- **AI 分析** — 根據跑步數據生成下週訓練課表

## 專案結構

```
aicoach.py          # 入口點：啟動 server + 開啟瀏覽器
src/
  server.py         # Flask API + 靜態檔案伺服器
  garmin_sync.py    # Garmin Connect 同步邏輯
  aicoach.py        # AI 教練分析邏輯（CLI 模式）
  dashboard.html    # 前端儀表板
  install.sh        # 依賴安裝腳本
data/               # 資料庫與暫存檔（自動建立）
  garmin_running_history.db
  ai_plan.json
  .garminconnect_token/
```
