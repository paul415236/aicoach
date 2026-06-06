#!/usr/bin/env python3
"""Entry point: starts the Flask server and opens the browser."""
import os
import sys
import threading
import webbrowser

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from server import app, DATA_DIR, DB_FILE
import sqlite3

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        activity_id INTEGER PRIMARY KEY, date TEXT, name TEXT,
        distance_km REAL, duration_mins REAL, elevation_gain_m REAL,
        avg_pace TEXT, avg_hr REAL, max_hr REAL, avg_cadence REAL,
        training_effect REAL, anaerobic_effect REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS activity_splits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id INTEGER,
        lap INTEGER, distance_km REAL, duration_mins REAL,
        avg_pace TEXT, avg_hr REAL, max_hr REAL, avg_cadence REAL,
        elevation_gain_m REAL)""")
    conn.commit()
    conn.close()

    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(debug=False, port=5000)
