"""ai_client.py 測試：模型清單、重試、fallback（mock 掉網路）。"""
import ai_client as A


def test_get_models_default(monkeypatch):
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)
    models = A.get_models()
    assert models == A.DEFAULT_MODELS
    assert models is not A.DEFAULT_MODELS  # 回傳副本，避免外部改到常數


def test_get_models_env_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODELS", "model-a, model-b ,, model-c")
    assert A.get_models() == ["model-a", "model-b", "model-c"]


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def test_call_ai_success(monkeypatch):
    def fake_post(*a, **k):
        return _Resp(200, {"choices": [{"message": {"content": "HELLO"}}]})
    monkeypatch.setattr(A._req, "post", fake_post)
    content, err = A.call_ai("prompt", "m", "key")
    assert content == "HELLO" and err is None


def test_call_ai_temperature(monkeypatch):
    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp(200, {"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(A._req, "post", fake_post)
    # 有給 temperature → payload 含 temperature
    A.call_ai("p", "m", "key", temperature=0.2)
    assert captured["json"].get("temperature") == 0.2
    # 不給 → payload 不含 temperature（用模型預設）
    A.call_ai("p", "m", "key")
    assert "temperature" not in captured["json"]


def test_call_ai_4xx_no_retry(monkeypatch):
    calls = {"n": 0}
    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp(400, text="bad request")
    monkeypatch.setattr(A._req, "post", fake_post)
    content, err = A.call_ai("prompt", "m", "key")
    assert content is None
    assert calls["n"] == 1          # 4xx 不重試
    assert "400" in err


def test_run_with_fallback_switches_model(monkeypatch):
    # 第一個模型失敗、第二個成功
    def fake_call(prompt_text, model, api_key, log=None, temperature=None):
        if model == "bad":
            return None, "500: boom"
        return "OK from " + model, None
    monkeypatch.setattr(A, "call_ai", fake_call)
    logs = []
    content = A.run_with_fallback("p", "key", models=["bad", "good"], log=logs.append)
    assert content == "OK from good"
    # 應記錄切換備援模型的訊息
    assert any("good" in m for m in logs)


def test_run_with_fallback_all_fail(monkeypatch):
    monkeypatch.setattr(A, "call_ai", lambda *a, **k: (None, "429: rate limited"))
    content = A.run_with_fallback("p", "key", models=["m1", "m2"], log=lambda _m: None)
    assert content is None
