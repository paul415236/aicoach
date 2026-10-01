"""pytest 共用設定：把 src/ 加入 import 路徑。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
