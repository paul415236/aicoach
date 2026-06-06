# AI Coach — Garmin Running AI Coach

[中文說明](README_CH.md)

> ⚠️ **Disclaimer**
>
> This project uses the unofficial third-party library (`garminconnect` / `garth`) to access Garmin Connect personal data. It is **not an officially authorized Garmin integration** and may violate [Garmin Connect Terms of Service](https://www.garmin.com/en-US/legal/terms-of-use/) §14 regarding automated access.
>
> - For **personal learning and research use only**. Not for commercial purposes.
> - Users assume all legal risks. The author is not responsible for any account suspension or loss.
> - This project is **not affiliated with Garmin Ltd.**

---

Automatically sync Garmin Connect running data, analyze fitness via AI, and generate a personalized marathon training plan — visualized in a Web Dashboard.

---

## Features

- Sync running history and lap data from Garmin Connect to local SQLite
- Generate next week's training plan (E/M/T/I pace zones) via OpenRouter AI
- Choose training philosophy: **Hansons**, **Jack Daniels**, or **Lydiard**
- Set history lookback period for analysis (1, 3, 6, 12 months)
- Set fixed rest days and LSD long run days
- Add free-text notes (injuries, race schedule, etc.) before generating the plan
- **Chinese / English** switchable AI output
- Flask Web Dashboard with run trends, lap pace charts, and AI schedule

---

## Screenshots

| Training Log | AI Coach Plan |
|:---:|:---:|
| ![Training Log](training_log.png) | ![AI Coach Plan](coach_plan.png) |

---

## Quick Start

### 1. Install dependencies

```bash
bash install.sh
```

### 2. Configure environment variables

Create a `.env` file:

```env
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=yourpassword
OPENROUTER_API_KEY=sk-or-...
```

> Get a free `OPENROUTER_API_KEY` at [https://openrouter.ai/](https://openrouter.ai/) → **Keys**.

### 3. Run

```bash
python aicoach.py
```

Opens the dashboard at `http://localhost:5000` automatically.

---

## Usage

| Button | Action |
|---|---|
| 🔄 Sync Garmin | Pull last 12 months of running records from Garmin Connect |
| 🤖 AI Analysis | Open the settings modal and generate next week's training plan |

**AI Analysis Modal options:**
- **Training Philosophy** — Daniels / Hansons / Lydiard
- **History Lookback** — choose how many months of past data the AI should analyze
- **Rest Days** — multi-select days of the week (no runs scheduled)
- **LSD Days** — multi-select days for long slow distance runs
- **Notes** — free-text context for the AI (injuries, goals, upcoming races)
- **Language** — 中文 / EN output toggle
- Settings are auto-saved and restored on next open

---

## Project Structure

```
aicoach.py               # Entry point: start server + open browser
src/
  server.py              # Flask API + static file server
  garmin_sync.py         # Garmin Connect sync logic
  dashboard.html         # Frontend dashboard
install.sh               # Dependency install script
data/                    # Auto-created at runtime
  garmin_running_history.db
  ai_plan.json
  analyze_config.json    # Saved AI modal settings
  .garminconnect_token/
```
