@echo off
chcp 65001 >nul
setlocal

REM 切換到專案根目錄（此 .bat 在 windows\ 子資料夾，spec 在根目錄）
cd /d "%~dp0.."

echo ============================================
echo    Garmin AI Coach - 打包成單一 exe
echo    （給開發者／打包者使用，不是給一般使用者）
echo ============================================
echo.

REM --- 確認目前位於專案根目錄（必須找得到 aicoach.spec）---
if not exist "aicoach.spec" (
    echo [錯誤] 找不到 aicoach.spec，目前目錄不是專案根目錄。
    echo 目前目錄： %CD%
    echo.
    echo 請「直接雙擊」windows\build.bat 執行，不要把內容複製貼到命令視窗。
    echo （若要手動執行：先 cd 到有 aicoach.py 的那層，再執行 py -m PyInstaller aicoach.spec）
    echo.
    pause
    exit /b 1
)

REM --- 找出可用的 Python 指令 ---
set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    where python >nul 2>nul && set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo [錯誤] 偵測不到 Python！請先安裝 Python 並勾選「Add Python to PATH」。
    echo.
    pause
    exit /b 1
)

echo [1/3] 安裝打包工具與依賴套件...
echo.
%PYTHON_CMD% -m pip install --upgrade pip
%PYTHON_CMD% -m pip install pyinstaller
%PYTHON_CMD% -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [錯誤] 安裝失敗，請確認網路連線後重試。
    pause
    exit /b 1
)

echo.
echo [2/3] 清除舊的打包輸出...
REM 先關閉可能正在執行的舊 exe，否則 dist 檔案被鎖住無法覆寫
taskkill /f /im AiCoach.exe >nul 2>nul
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo.
echo [3/3] 開始打包（pyinstaller aicoach.spec）...
echo.
%PYTHON_CMD% -m PyInstaller aicoach.spec
if %errorlevel% neq 0 (
    echo.
    echo [錯誤] 打包失敗，請把上方訊息截圖尋求協助。
    pause
    exit /b 1
)

echo.
echo ============================================
echo    打包完成！
echo ============================================
echo.
echo 產出檔案： dist\AiCoach.exe
echo.
echo 交付給使用者時，請一併提供：
echo   1. dist\AiCoach.exe
echo   2. .env.example （改名為 .env 並填入帳號/API Key）
echo 兩個檔案放在「同一個資料夾」即可，雙擊 AiCoach.exe 執行。
echo （程式會在 exe 同目錄自動建立 data 資料夾存放資料庫與課表）
echo.
pause
