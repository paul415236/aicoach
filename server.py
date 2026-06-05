#!/usr/bin/env python3
import json
import sqlite3
from flask import Flask, send_from_directory

app = Flask(__name__)
DB_FILE = "garmin_running_history.db"
AI_PLAN_FILE = "ai_plan.json"

def query(sql, args=()):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def json_resp(data):
    return app.response_class(
        response=json.dumps(data, ensure_ascii=False),
        mimetype='application/json; charset=utf-8'
    )

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/api/runs")
def get_runs():
    return json_resp(query("SELECT * FROM runs ORDER BY date DESC"))

@app.route("/api/runs/<int:activity_id>/splits")
def get_splits(activity_id):
    return json_resp(query(
        "SELECT * FROM activity_splits WHERE activity_id=? ORDER BY lap",
        (activity_id,)
    ))

@app.route("/api/ai-plan")
def get_ai_plan():
    import os
    if not os.path.exists(AI_PLAN_FILE):
        return json_resp({"content": None, "generated_at": None})
    with open(AI_PLAN_FILE, encoding="utf-8") as f:
        return json_resp(json.load(f))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
