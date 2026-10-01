@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM 切換到專案根目錄（此 .bat 在 windows\ 子資料夾，故往上一層）
cd /d "%~dp0.."

echo ============================================
echo    Garmin AI Coach - 設定帳號
echo ============================================
echo.
echo 這個精靈會幫你建立設定檔 (.env)，
echo 請依序填入下列三項資訊：
echo.

REM 若已存在 .env，先確認是否覆蓋
if exist ".env" (
    echo [提醒] 偵測到已有設定檔 .env。
    set /p "OVERWRITE=要重新設定並覆蓋嗎？(Y=是 / N=否)： "
    if /i not "!OVERWRITE!"=="Y" (
        echo 已取消，保留原本的設定。
        echo.
        pause
        exit /b 0
    )
    echo.
)

echo --------------------------------------------
echo [1/3] Garmin Connect 登入 Email
echo       （就是你登入 Garmin Connect 用的電子郵件）
echo --------------------------------------------
set /p "GARMIN_EMAIL=請輸入 Email： "
echo.

echo --------------------------------------------
echo [2/3] Garmin Connect 密碼
echo       （注意：輸入時螢幕上會顯示文字，請確認旁邊沒有人）
echo --------------------------------------------
set /p "GARMIN_PASSWORD=請輸入密碼： "
echo.

echo --------------------------------------------
echo [3/3] OpenRouter API Key
echo       （免費申請：https://openrouter.ai/ 登入後點 Keys）
echo       （格式類似 sk-or-xxxxxxxx...）
echo --------------------------------------------
set /p "OPENROUTER_API_KEY=請貼上 API Key： "
echo.

REM 基本檢查：三項皆不可為空
if "!GARMIN_EMAIL!"=="" goto :empty_error
if "!GARMIN_PASSWORD!"=="" goto :empty_error
if "!OPENROUTER_API_KEY!"=="" goto :empty_error

REM 用 PowerShell 寫檔，可正確處理密碼/金鑰中的特殊字元，並存成 UTF-8（不含 BOM）
REM 整段寫成單行，避免 batch 的 ^ 續行符在 CRLF 檔案下失效
powershell -NoProfile -Command "$lines = @('GARMIN_EMAIL=' + $env:GARMIN_EMAIL, 'GARMIN_PASSWORD=' + $env:GARMIN_PASSWORD, 'OPENROUTER_API_KEY=' + $env:OPENROUTER_API_KEY); [System.IO.File]::WriteAllLines((Join-Path (Get-Location) '.env'), $lines, (New-Object System.Text.UTF8Encoding $false))"

if %errorlevel% neq 0 (
    echo [錯誤] 寫入設定檔失敗，請尋求協助。
    echo.
    pause
    exit /b 1
)

echo ============================================
echo    設定完成！已建立 .env 設定檔
echo ============================================
echo.
echo 下一步：雙擊「start.bat」啟動程式。
echo.
pause
exit /b 0

:empty_error
echo.
echo [錯誤] 有欄位沒有填寫，三項都必須填入。請重新執行本精靈。
echo.
pause
exit /b 1
