# AI Coach — Garmin 跑步 AI 教練

[English Version](README.md)

> ⚠️ **免責聲明**
>
> 本專案使用非官方第三方套件（`garminconnect` / `garth`）存取 Garmin Connect 個人數據，**並非 Garmin 官方授權之整合方案**，可能違反 [Garmin Connect 服務條款](https://www.garmin.com/en-US/legal/terms-of-use/)第 14 條關於自動化存取的規定。
>
> - 本專案**僅供個人學習與研究用途**，不得用於商業目的。
> - 使用者須自行承擔所有法律風險，作者不對任何損失或帳號停權負責。
> - 本專案與 Garmin Ltd. **無任何關聯**。

---

自動同步 Garmin Connect 跑步數據，透過 AI 分析體能狀態並生成個人化馬拉松訓練課表，並以 Web Dashboard 視覺化呈現。

---

## 功能

- 從 Garmin Connect 同步跑步歷史和圈數數據到本地 SQLite 資料庫
- 透過 OpenRouter AI 生成下週訓練計畫（E/M/T/I 配速區間）
- 選擇訓練哲學：**Hansons (漢森)**、**Jack Daniels (丹尼爾)** 或 **Lydiard (利迪亞德)**
- 設定固定休息日和 LSD 長跑日
- 在生成計畫前加入文字備註（傷痛情況、賽事安排等）
- 支持 **中文 / 英文** 切換的 AI 輸出
- 提供 Flask Web Dashboard，包含跑步趨勢、單圈配速圖表和 AI 訓練課表

---

## 截圖

| 訓練紀錄 | AI 訓練建議 |
|:---:|:---:|
| ![訓練紀錄](training_log.png) | ![AI 訓練建議](coach_plan.png) |

---

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

> 請至 [https://openrouter.ai/](https://openrouter.ai/) 的 **Keys** 頁面獲取免費的 `OPENROUTER_API_KEY`。

### 3. 啟動

```bash
python aicoach.py
```

程式會自動開啟瀏覽器並導向 `http://localhost:5000`。

---

## 使用方式

| 按鈕 | 動作 |
|---|---|
| 🔄 同步 Garmin | 從 Garmin Connect 抓取過去 12 個月的跑步紀錄 |
| 🤖 AI 分析 | 開啟設定視窗並生成下週訓練計畫 |

**AI 分析設定選項：**
- **訓練哲學 (Training Philosophy)** — Daniels / Hansons / Lydiard
- **歷史紀錄參考範圍 (History Lookback)** — 選擇要參考過去幾個月的數據進行分析
- **休息日 (Rest Days)** — 複選每週不安排跑步的日子
- **LSD 日 (LSD Days)** — 複選安排長距離慢跑的日子
- **備註 (Notes)** — 提供給 AI 的額外背景資訊（傷痛、目標、近期賽事）
- **語言 (Language)** — 中文 / 英文 輸出切換
- 設定會自動儲存，下次開啟時會自動帶入

---

## 專案結構

```
aicoach.py               # 入口程式：啟動伺服器並開啟瀏覽器
src/
  server.py              # Flask API 與靜態檔案伺服器
  garmin_sync.py         # Garmin Connect 同步邏輯
  dashboard.html         # 前端儀表板
install.sh               # 依賴安裝腳本
data/                    # 執行時自動建立
  garmin_running_history.db
  ai_plan.json
  analyze_config.json    # 儲存的 AI 設定
  .garminconnect_token/
```
