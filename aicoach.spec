# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包設定：把 Garmin AI Coach 打包成單一 exe。

產出：dist/AiCoach.exe（Windows 上執行 `pyinstaller aicoach.spec` 產生）。

重點：
- 進入點 aicoach.py 會 sys.path.insert('src')，PyInstaller 靜態分析可能追不到
  src/ 下的模組，故這裡用 pathex 加入 src，並以 hiddenimports 明確列出。
- dashboard.html 是前端唯讀檔，打包進 bundle 根目錄；server.py 在 frozen 模式
  以 BASE_DIR = sys._MEIPASS 讀它（send_from_directory(BASE_DIR, 'dashboard.html')）。
- 使用者資料（.env / data/）放在 exe 同目錄、不打包進 bundle，故可讀寫。
"""
import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

SRC = os.path.join(os.getcwd(), "src")

# 第三方套件常有動態 import，整包收齊避免遺漏
hidden = []
for pkg in ("garminconnect", "garth", "flask", "dotenv", "requests"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass
# 專案自己的模組（因 aicoach.py 動態 insert src/ 到 path，靜態分析追不到）
hidden += ["server", "classifier", "prompts", "ai_client"]

a = Analysis(
    ["aicoach.py"],
    pathex=[SRC],                       # 讓 PyInstaller 找得到 src/ 下模組
    binaries=[],
    datas=[
        (os.path.join("src", "dashboard.html"), "."),  # → bundle 根目錄
        ("VERSION", "."),                              # 版號檔 → bundle 根目錄
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AiCoach",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # 保留主控台視窗（顯示啟動訊息/錯誤，與現有 .bat 體驗一致）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="windows/icon.ico",  # 如有圖示可取消註解
)
