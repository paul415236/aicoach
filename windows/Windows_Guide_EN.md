# Windows Easy Setup

A step-by-step guide to running Garmin AI Coach on Windows.
Everything is done by double-clicking a few files — just follow along.

> ⚠️ Please read the disclaimer at the top of the README before use. This is an
> unofficial, personal/research tool that accesses Garmin account data via
> third-party libraries, which may violate Garmin's Terms of Service. Use at your own risk.

> 繁體中文版請見 [`Windows使用說明.md`](Windows使用說明.md)

---

## Two options — pick one

- **Option A (easiest, recommended): standalone exe** — no Python needed; just
  double-click `AiCoach.exe`. See "Option A" below.
- **Option B: Python version** — install Python yourself; for those who want to
  read or modify the source. See "Option B" below.

---

## Option A: standalone exe (recommended)

You'll receive two files (keep them **in the same folder**):
- `AiCoach.exe`
- `.env.example` (a config template — you'll **rename it to `.env`** and fill in
  your account and API key)

Steps:

1. **Get a free OpenRouter API Key**
   (the "key" the AI uses to analyze your training — free, ~2 min)

   1-1. Open <https://openrouter.ai/>
   1-2. Click "**Sign In**" (top right); signing in with **Google** is fastest.
   1-3. Click your avatar (top right) → "**Keys**" (or go to <https://openrouter.ai/keys>)
   1-4. Click the blue "**Create Key**" button
   1-5. Give it any name (e.g. `aicoach`) and click "**Create**"
   1-6. A long string starting with **`sk-or-`** appears → click "**Copy**"
         ⚠️ It is shown in full only once — copy it now; you can't see the full value after closing.

2. **Put your account details into the config file**

   2-1. Find the **`.env.example`** file next to `AiCoach.exe`
   2-2. **Rename it to `.env`** (right-click → Rename → set the whole name to `.env`,
        including the leading dot)

        > ⚠️ Windows hides file extensions by default, so you may only see `env` or
        > `env.example`. In File Explorer, tick "**File name extensions**" under the
        > "View" menu, and make sure the final name is exactly **`.env`**
        > (not `.env.txt`, not `.env.example`).

   2-3. Right-click `.env` → "**Open with**" → "**Notepad**"
   2-4. Edit the three lines with your own values (no spaces around `=`):
        ```
        GARMIN_EMAIL=your Garmin login email (full, incl. .com)
        GARMIN_PASSWORD=your Garmin password
        OPENROUTER_API_KEY=sk-or-the string you copied
        ```
   2-5. Press **Ctrl+S** to save, then close Notepad.

   > ⚠️ Common mistakes: use the **full** email (e.g. `abc@gmail.com`, don't drop `.com`);
   > no spaces around `=`; fill in all three lines.

3. **Double-click `AiCoach.exe`**
   - A black console window opens; after a few seconds your browser opens `http://localhost:5000`.
   - On first run, if a blue "**Windows protected your PC**" dialog appears, click
     "**More info**" → "**Run anyway**" (the exe is unsigned, not a virus).

> ★ Keep the black console window open while using the app — closing it stops the program.
> A `data` folder is created automatically next to the exe for your run database and plans; don't delete it.

Then you're ready — see "Using the app" at the bottom.

> 💡 For packagers: the exe is built from `aicoach.spec` in the project root. On a
> Windows machine with Python, double-click `windows\build.bat` to install PyInstaller
> and produce `dist\AiCoach.exe`.

---

## Option B: Python version

> These steps require Python. If you're using Option A (the exe), **skip Option B entirely.**

### What you need

1. A Windows 10 or Windows 11 PC
2. Your **Garmin Connect** account (email and password)
3. A free **OpenRouter API Key** (step 2 below shows how to get one)

### Step 1: Install Python (once)

1. Open <https://www.python.org/downloads/>
2. Click the big "**Download Python**" button
3. Run the installer
4. **【IMPORTANT】** At the bottom of the installer, tick ☑️ **"Add Python to PATH"**
5. Click "**Install Now**", wait, then "Close"

> 💡 Not sure if Python is already installed? Just try Step 3 — if it's missing you'll be told to come back here.

### Step 2: Get a free OpenRouter API Key

1. Open <https://openrouter.ai/>
2. Click "**Sign In**" (top right); Google sign-in is fastest.
3. Click your avatar → "**Keys**" (or go to <https://openrouter.ai/keys>)
4. Click "**Create Key**"
5. Give it any name (e.g. `aicoach`) and click "**Create**"
6. Copy the string starting with **`sk-or-`** (click "**Copy**")

> ⚠️ Shown in full only once — copy it now and paste it somewhere temporarily.

### Step 3: Install dependencies (once)

1. Open the **`windows`** subfolder in the project folder (the three `.bat` files are there)
2. Double-click **`install.bat`**
3. A console window installs everything — please wait
4. When you see "安裝完成 (Done)", press a key to close

> ❓ If it says Python wasn't found, Step 1 didn't take — reinstall and tick "Add Python to PATH".

### Step 4: Set up your account (once)

1. Double-click **`設定帳號.bat`** (Set up account)
2. Enter, when prompted:
   - **Garmin Email** — your Garmin Connect login email
   - **Garmin Password** — your Garmin password (shown on screen; mind onlookers)
   - **OpenRouter API Key** — paste the `sk-or-...` from Step 2 (right-click to paste)
3. When you see "設定完成 (Done)", press a key to close

> 🔒 Your credentials are only stored in the `.env` file on your own computer; nothing is uploaded.

### Step 5: Launch the app (every time)

1. Double-click **`start.bat`**
2. A console window opens; your browser opens automatically (if not, go to `http://localhost:5000`)
3. You're in!

> ★ Keep the black console window open; closing it stops the program.

---

## Using the app: two buttons

| Button | What it does |
|---|---|
| 🔄 Sync Garmin | Pull the last 12 months of runs from Garmin Connect |
| 🤖 AI Analysis | Open the settings modal and generate next week's plan |

- First time, click "🔄 Sync Garmin" to pull your data.
- If Garmin asks for an **MFA code**, an input box appears on the page — enter the code
  from your phone/email.
- After syncing, click "🤖 AI Analysis", choose a training approach and target race, and generate a plan.

---

## FAQ

**Q: The console window flashes and disappears?**
A: These files pause for a keypress at the end, so this shouldn't happen. If it does,
   it's usually antivirus blocking it — allow it, or ask for help.

**Q: A long red English error appears?**
A: Screenshot the console contents and note which step you were on, then ask for help.

**Q: AI analysis fails / says all models failed / 429?**
A: Free AI models get rate-limited when busy. Wait a few minutes and click "🤖 AI Analysis" again.

**Q: Sync Garmin shows "429 Too Many Requests"?**
A: Garmin's server is rate-limiting you after too many logins in a short time.
   **Wait 15-30 minutes** and try again; don't repeatedly hit sync.

**Q: Sync Garmin shows "401 Unauthorized"?**
A: Wrong account or password. The most common cause is an **incomplete email**
   (e.g. missing `.com`). Open `.env` and check `GARMIN_EMAIL` is the full address,
   the password is correct, and there are no spaces around `=`.

**Q: How do I change my Garmin password or API key later?**
A:
   - exe version: open `.env` in Notepad, edit, save.
   - Python version: double-click `設定帳號.bat` again; it asks to overwrite, then re-enter.

**Q: Do I repeat all steps every time?**
A: No. Steps 1-4 are one-time. After that, just double-click `start.bat` (Python) or
   `AiCoach.exe` (exe version).
