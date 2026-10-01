@echo off
chcp 65001 >nul
setlocal

REM 切換到專案根目錄（此 .bat 在 windows\ 子資料夾，故往上一層）
cd /d "%~dp0.."

echo ============================================
echo    Garmin AI Coach - 啟動中
echo ============================================
echo.

REM --- 找出可用的 Python 指令 ---
set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    where python >nul 2>nul && set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo [錯誤] 偵測不到 Python！
    echo 請先雙擊執行「install.bat」完成安裝。
    echo.
    pause
    exit /b 1
)

REM --- 檢查 .env 設定檔是否存在 ---
if not exist ".env" (
    echo [提醒] 找不到設定檔 .env！
    echo 請先雙擊執行「設定帳號.bat」填入你的 Garmin 帳號與 API Key。
    echo.
    pause
    exit /b 1
)

echo 伺服器啟動後，瀏覽器會自動開啟 http://localhost:5000
echo 若沒有自動開啟，請手動在瀏覽器輸入上面的網址。
echo.
echo ★ 使用期間請「不要關閉這個黑色視窗」，關閉視窗等於關閉程式。
echo ============================================
echo.

%PYTHON_CMD% aicoach.py

REM 程式結束或發生錯誤時停在這裡，讓使用者看得到訊息
echo.
echo ============================================
echo 程式已結束。若上方出現紅色錯誤訊息，請截圖尋求協助。
echo ============================================
pause
