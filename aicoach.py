#!/usr/bin/env python3
"""Entry point: starts the Flask server and opens the browser."""
import os
import sys

# 停用 pydantic 外掛（必須在 import pydantic 之前）。
# garth 會間接引入 logfire 的 pydantic plugin，它在打包成 exe（frozen）時會用
# inspect.getsource() 讀自身原始碼而失敗（OSError: could not get source code）。
# 我們沒用到 logfire，直接停用所有 pydantic plugin 最乾淨。
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "true")

import threading
import webbrowser

# 開發模式才需要把 src/ 加進 path；打包成 exe（frozen）時模組已在 bundle 內
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from server import app, DATA_DIR, DB_FILE, ensure_schema
import sqlite3

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    ensure_schema()

    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(debug=False, port=5000)
