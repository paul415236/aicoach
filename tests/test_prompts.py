"""prompts.py 測試：術語正規化與流派規則完整性。"""
import prompts as P


def test_normalize_terms_yigan():
    assert P.normalize_terms("易感跑配速") == "輕鬆跑配速"
    assert P.normalize_terms("易感帶") == "輕鬆跑配速帶"
    assert P.normalize_terms("易感區間") == "輕鬆區間"


def test_normalize_terms_tilde_in_range():
    # 數值範圍的半形 ~ 轉全形 ～
    assert P.normalize_terms("150~160 bpm") == "150～160 bpm"
    assert P.normalize_terms("4:30~4:40/km") == "4:30～4:40/km"


def test_normalize_terms_tilde_not_in_range():
    # 一般中文旁的 ～ 不應被動到（非數字/字母夾住）
    assert P.normalize_terms("一般文字～結尾") == "一般文字～結尾"


def test_normalize_terms_empty():
    assert P.normalize_terms("") == ""
    assert P.normalize_terms(None) is None


def test_coach_rules_keys():
    for d in (P.COACH_RULES_ZH, P.COACH_RULES_EN):
        assert set(d.keys()) == {"daniels", "hansons", "lydiard"}
        for v in d.values():
            assert isinstance(v, str) and len(v) > 100


def test_terminology_nonempty():
    assert "輕鬆跑" in P.TERMINOLOGY_ZH
    assert "Easy" in P.TERMINOLOGY_EN


def test_hansons_rule_mentions_mp_equals_tempo():
    # Hansons 的核心：tempo = MP，規則內必須提到
    assert "馬拉松配速" in P.COACH_RULES_ZH["hansons"]
    assert "Marathon Pace" in P.COACH_RULES_EN["hansons"]


# ── extract_schedule / strip_schedule_json ────────────────
_RAW_WITH_JSON = (
    "分析文字與課表...\n\n```json\n"
    '{"weekly_schedule": ['
    '{"day": "週一", "type": "easy", "distance_km": 10, "pace": "5:30", "hr": "140-150", "description": "輕鬆"},'
    '{"day": "週二", "type": "rest", "distance_km": 0, "pace": "", "hr": "", "description": "休息"},'
    '{"day": "週三", "type": "weird", "distance_km": "x"}'
    "]}\n```\n"
)


def test_extract_schedule_ok():
    sched = P.extract_schedule(_RAW_WITH_JSON)
    assert sched is not None and len(sched) == 3
    assert sched[0]["type"] == "easy" and sched[0]["distance_km"] == 10.0
    assert sched[1]["type"] == "rest"
    # 非法 type 退回 easy，非法 distance 退回 0
    assert sched[2]["type"] == "easy" and sched[2]["distance_km"] == 0.0


def test_extract_schedule_none_on_bad_input():
    assert P.extract_schedule("沒有 json 區塊") is None
    assert P.extract_schedule("") is None
    assert P.extract_schedule("```json\n{bad json}\n```") is None


def test_strip_schedule_json():
    stripped = P.strip_schedule_json(_RAW_WITH_JSON)
    assert "```json" not in stripped
    assert "分析文字與課表" in stripped
