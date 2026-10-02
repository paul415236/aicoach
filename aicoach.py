#!/usr/bin/env python3
"""Entry point: starts the Flask server and opens the browser."""
import os
import sys
import threading
import webbrowser

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from server import app, DATA_DIR, DB_FILE, ensure_schema
import sqlite3

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    ensure_schema()

    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(debug=False, port=5000)
