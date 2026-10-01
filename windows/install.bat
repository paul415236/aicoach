@echo off
chcp 65001 >nul
setlocal

echo ============================================
echo    Garmin AI Coach - 安裝依賴套件
echo ============================================
echo.

REM --- 找出可用的 Python 指令 (優先 py，再試 python) ---
set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    where python >nul 2>nul && set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo [錯誤] 偵測不到 Python！
    echo.
    echo 請先安裝 Python，步驟：
    echo   1. 前往 https://www.python.org/downloads/
    echo   2. 下載並安裝最新版 Python
    echo   3. 安裝時務必勾選「Add Python to PATH」
    echo   4. 安裝完成後，重新執行這個 install.bat
    echo.
    pause
    exit /b 1
)

echo [1/2] 偵測到 Python，版本資訊：
%PYTHON_CMD% --version
echo.

echo [2/2] 開始安裝依賴套件（flask / garminconnect / garth / python-dotenv / requests）...
echo.
%PYTHON_CMD% -m pip install --upgrade pip
%PYTHON_CMD% -m pip install flask garminconnect garth python-dotenv requests

if %errorlevel% neq 0 (
    echo.
    echo [錯誤] 套件安裝失敗！
    echo 請確認網路連線正常後，再重新執行 install.bat。
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo    安裝完成！
echo ============================================
echo.
echo 下一步：
echo   1. 雙擊「設定帳號.bat」填入你的 Garmin 帳號與 API Key
echo   2. 雙擊「start.bat」啟動程式
echo.
pause
