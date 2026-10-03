"""bump_version.py 的版號遞增邏輯測試。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bump_version as B


def test_bump_patch():
    assert B.bump("1.0.0", "patch") == "1.0.1"
    assert B.bump("1.2.9", "patch") == "1.2.10"


def test_bump_minor():
    assert B.bump("1.0.5", "minor") == "1.1.0"   # patch 歸零
    assert B.bump("2.9.9", "minor") == "2.10.0"


def test_bump_major():
    assert B.bump("1.5.3", "major") == "2.0.0"   # minor/patch 歸零


def test_bump_invalid_current():
    import pytest
    with pytest.raises(ValueError):
        B.bump("1.0", "patch")      # 非 x.y.z
    with pytest.raises(ValueError):
        B.bump("1.0.0", "huge")     # 未知遞增類型
