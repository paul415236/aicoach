"""OpenRouter AI 呼叫：單模型重試 + 多模型 fallback（無 Flask 依賴）。

對外介面：
- get_models()：回傳要依序嘗試的模型清單（可由環境變數 OPENROUTER_MODELS 覆寫）
- call_ai(prompt_text, model, api_key, log=None)：對單一模型呼叫，遇 429/5xx 指數退避重試
- run_with_fallback(prompt_text, api_key, models=None, log=None, tr=None, lang="zh")：
  依序嘗試多個模型，成功回傳 content，全部失敗回傳 None

log 為可選的訊息 callback（接受一個字串）；tr 為可選的雙語函式 tr(lang, zh, en)，
未提供時以中文訊息為預設，讓本模組可獨立於 server 使用與測試。
"""
import os
import time as _time

import requests as _req


DEFAULT_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "cohere/north-mini-code:free",
    "dots-studio/dots-3-note-preview:free",
    "poolside/laguna-s-2.1:free",
    "inclusionai/ling-3.0-flash-sante:free",
]


def get_models():
    """回傳模型清單：OPENROUTER_MODELS（逗號分隔）優先，否則用內建預設。"""
    models_env = os.getenv("OPENROUTER_MODELS", "").strip()
    if models_env:
        return [m.strip() for m in models_env.split(",") if m.strip()]
    return list(DEFAULT_MODELS)


def _noop_log(_msg):
    pass


def call_ai(prompt_text, model, api_key, log=None):
    """對單一 model 呼叫，遇 429/5xx 以指數退避重試。回傳 (content, None) 或 (None, err_msg)。"""
    log = log or _noop_log
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = _req.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt_text}]},
                timeout=120
            )
        except Exception as e:
            return None, f"連線錯誤: {e}"

        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception as e:
                return None, f"回應解析失敗: {e} / {resp.text[:200]}"
            # OpenRouter 有時回 HTTP 200 但 body 內含 error（如上游 503/429 overloaded）
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                ecode = err.get("code")
                emsg = err.get("message", "")[:150]
                # 上游暫時性錯誤（429/5xx / overloaded）→ 退避重試
                transient = ecode in (429, 500, 502, 503, 504) or \
                    (err.get("metadata", {}) or {}).get("error_type") == "provider_overloaded"
                if transient and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    log(f"⏳ {model} 上游錯誤 {ecode}，{wait}s 後重試 "
                        f"({attempt + 1}/{max_retries - 1})...")
                    _time.sleep(wait)
                    continue
                return None, f"{ecode}: {emsg}"
            try:
                return body["choices"][0]["message"]["content"], None
            except Exception as e:
                return None, f"回應解析失敗: {e} / {resp.text[:200]}"

        # 429（限流）或 5xx（上游暫時性錯誤）→ 退避後重試
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                log(f"⏳ {model} 回 {resp.status_code}，{wait}s 後重試 "
                    f"({attempt + 1}/{max_retries - 1})...")
                _time.sleep(wait)
                continue
            return None, f"{resp.status_code}: {resp.text[:200]}"

        # 其他錯誤（4xx）不重試，直接放棄此 model
        return None, f"{resp.status_code}: {resp.text[:200]}"
    return None, "重試次數用盡"


def run_with_fallback(prompt_text, api_key, models=None, log=None, tr=None, lang="zh"):
    """依序嘗試 models，成功回傳 content，全部失敗回傳 None。"""
    log = log or _noop_log
    if tr is None:
        tr = lambda _lang, zh, _en: zh  # noqa: E731 預設用中文訊息
    if models is None:
        models = get_models()
    last_err = ""
    for i, model in enumerate(models):
        if i > 0:
            log(tr(lang, f"🔀 切換備援模型: {model}", f"🔀 Switching to fallback model: {model}"))
        content, err = call_ai(prompt_text, model, api_key, log=log)
        if content is not None:
            if i > 0:
                log(tr(lang, f"✅ 使用備援模型 {model} 成功",
                       f"✅ Fallback model {model} succeeded"))
            return content
        last_err = err
        log(tr(lang, f"⚠️ {model} 失敗: {err}", f"⚠️ {model} failed: {err}"))
    log(tr(lang, f"❌ 所有模型皆呼叫失敗，最後錯誤: {last_err}",
           f"❌ All models failed, last error: {last_err}"))
    log(tr(lang,
           "💡 免費模型常因上游限流回 429，請稍候再試，或於 .env 設定 "
           "OPENROUTER_MODELS 指定其他模型 / 使用付費模型。",
           "💡 Free models are often rate-limited (429). Retry later, or set "
           "OPENROUTER_MODELS in .env to use other/paid models."))
    return None
