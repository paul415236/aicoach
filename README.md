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

### 3. 同步 Garmin 數據

```bash
python garmin_sync.py
```

### 4. 生成 AI 訓練課表

```bash
python aicoach.py
```

課表會輸出至終端機並儲存為 `ai_plan.json`。

### 5. 啟動 Web 儀表板

```bash
python server.py
```

開啟瀏覽器訪問 `http://localhost:5000`

## 檔案說明

| 檔案 | 說明 |
|------|------|
| `garmin_sync.py` | 登入 Garmin Connect，同步跑步數據到 SQLite |
| `aicoach.py` | 讀取 DB，呼叫 AI 生成課表 |
| `server.py` | Flask API + 靜態檔案伺服器 |
| `dashboard.html` | 前端儀表板 |
| `garmin_running_history.db` | 本地 SQLite 資料庫 |
| `ai_plan.json` | AI 生成的最新課表 |

## 訓練目標

全馬 Sub 2:54（目標配速 4:04/km），採用丹尼爾博士科學化跑步方程式，每週 5 天訓練。
