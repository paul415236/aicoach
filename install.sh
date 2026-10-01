#!/bin/bash
set -e

echo "📦 安裝 AI Coach 依賴套件..."
pip install flask garminconnect garth python-dotenv requests

echo "🧪 安裝測試套件（選用，執行 pytest 用）..."
pip install pytest

echo "✅ 安裝完成！"
echo "➡️  請建立 .env 檔案並執行 python aicoach.py"
