"""AI prompt 的靜態素材與術語處理（無 Flask / 外部服務依賴）。

集中：
- TERMINOLOGY_ZH / TERMINOLOGY_EN：六種標準區間術語規範
- COACH_RULES_ZH / COACH_RULES_EN：Daniels / Hansons / Lydiard 三流派配速區間定義
- normalize_terms：對 AI 輸出做確定性術語正規化

prompt 的動態組裝（依跑者框架、賽事、減量期等）仍在 server.py 的 analyze route，
因為它與該 route 的區域變數緊密耦合；本模組只負責可重用的靜態素材與後處理。
"""
import re as _re


TERMINOLOGY_ZH = """【術語建議（請盡量統一使用下列六種標準中文名稱，避免自創詞如「易感跑」）】
1. 「輕鬆跑」= Easy = E。
2. 「馬拉松配速跑」= Marathon Pace = M。
3. 「節奏跑」= Tempo = Threshold = T。
4. 「短間歇」= 單趟距離「小於 1600m」的間歇（如 400m/600m/800m/1000m 反覆），配速最快。
5. 「長間歇」= 單趟距離「1600m 以上」的間歇（如 1600m/2000m 反覆），配速略慢於短間歇。
6. 「長跑」= Long Run = LSD。
* 間歇課建議依單趟距離歸為「短間歇」或「長間歇」，避免使用「易感跑」等自創詞。"""

TERMINOLOGY_EN = """[Terminology rules — use ONLY these six zone names consistently; do NOT invent synonyms]
1. Easy (E)
2. Marathon Pace (M)
3. Tempo / Threshold (T)
4. Short Interval — reps shorter than 1600m (e.g. 400m/600m/800m/1000m), fastest pace
5. Long Interval — reps 1600m or longer (e.g. 1600m/2000m), slightly slower than short intervals
6. Long Run (LSD)
* Do not use generic "Interval"/"Rep"; classify every interval workout as Short Interval or Long Interval by rep distance.
* Pick one label per zone and use it consistently; do not alternate between synonyms."""


# ── 三流派配速區間定義（靜態；依 coach key 取用）────────────────
COACH_RULES_EN = {
    "daniels": """[Jack Daniels — Pace Zone Definitions (derive from VDOT, cross-check with the athlete's actual HR/pace data; do NOT just add/subtract fixed seconds from MP)]
* E (Easy): ~MP + 0:60 to 1:30 per km. 65-79% HRmax. Base/recovery/warm-up.
* M (Marathon): = goal MP. 80-89% HRmax.
* T (Threshold/Tempo): faster than MP by only ~0:10-0:20 per km — pace you can hold ~1 hour ("comfortably hard"). 88-92% HRmax. Tempo runs (20 min) or cruise intervals.
* Long Interval (>=1600m, VO2max): faster than T. 3-5 min reps at ~95-100% HRmax.
* Short Interval (<1600m, speed): fastest, short reps (200-1000m) for speed/economy; not HR-driven.
STRICT ordering (slow→fast): Easy > Marathon Pace > Tempo > Long Interval > Short Interval.""",
    "hansons": """[Hansons — Pace Zone Definitions (KEY: Hansons "tempo" = Marathon Pace, NOT faster than MP)]
* Easy: MP + 1:00 to 2:00 per km. 60-70% HRmax. Most weekly volume runs here.
* Tempo run (Hansons SOS): run AT Marathon Pace (= goal MP, e.g. 4:04/km). This is the signature workout, building up toward ~16 km at MP. Do NOT make it faster than MP.
* Strength (SOS): slightly faster than MP by ~0:06-0:10 per km only (e.g. ~10K-to-HM effort), simulating late-race fatigue.
* Speed (SOS, early cycle): 5K-3K race effort intervals, faster than strength.
* Long run: run at EASY pace (same zone as easy days, NOT faster than easy), never exceed ~16 miles (~26 km); rely on cumulative fatigue, not distance.
STRICT ordering (slow→fast): Long ≈ Easy > Tempo(=MP) > Strength > Speed.
Common mistake to AVOID: setting Tempo faster than MP, or making the Long run faster than Easy.""",
    "lydiard": """[Lydiard — Pace Zone Definitions (aerobic-base first, drive by feel/HR more than fixed pace math)]
* Aerobic/base runs: comfortable aerobic effort, ~MP + 0:60 to 1:45 per km, well below anaerobic threshold. 70-80% HRmax. This is the bulk of base phase.
* Steady/aerobic threshold: near but below threshold, ~MP + 0:10-0:30 per km.
* Hill phase: hill bounding/springing for strength, effort-based, not a road pace.
* Anaerobic/track phase (later): intervals & time trials faster than MP for sharpening.
* Long run: aerobic effort, same easy zone (NOT faster than easy).
STRICT ordering (slow→fast): Long ≈ Aerobic base > Steady > MP > Track intervals.
Prioritize aerobic development; do not prescribe fast paces during the base phase.""",
}

COACH_RULES_ZH = {
    "daniels": """【Jack Daniels — 配速區間定義（請由 VDOT 推算，並用跑者實際心率/配速交叉驗證；不要只用「MP 加減固定秒數」硬套）】
* E 輕鬆跑（Easy）：約 MP + 0:60 ～ 1:30/km。65-79% HRmax。基礎、恢復、熱身/冷卻。
* M 馬拉松配速（Marathon）：= 目標 MP。80-89% HRmax。
* T 節奏/閾值跑（Threshold/Tempo）：只比 MP 快約 0:10 ～ 0:20/km，是「可維持約 1 小時」的「舒適的辛苦」配速。88-92% HRmax。20 分鐘 tempo 或巡航間歇。
* 長間歇（>=1600m，VO2max）：比節奏跑更快，3-5 分鐘反覆，約 95-100% HRmax。
* 短間歇（<1600m，速度）：最快，200-1000m 短反覆，練速度/跑步經濟性；不以心率為準。
嚴格由慢到快：輕鬆跑 ＞ 馬拉松配速跑 ＞ 節奏跑 ＞ 長間歇 ＞ 短間歇。""",
    "hansons": """【Hansons — 配速區間定義（重點：Hansons 的「tempo」＝馬拉松配速，不是比 MP 快！）】
* 輕鬆跑（Easy）：MP + 1:00 ～ 2:00/km。60-70% HRmax。每週大部分里程都在此。
* Tempo 跑（Hansons 招牌 SOS 課）：就以「馬拉松配速」跑（＝目標 MP，例如 4:04/km），逐步累積到約 16 km @ MP。絕對不要比 MP 快。
* 力量課（Strength, SOS）：只比 MP 快約 0:06 ～ 0:10/km（約 10K～半馬強度），模擬比賽後段疲勞。
* 速度課（Speed, SOS，週期早段）：5K-3K 比賽強度的間歇，比力量課快。
* 長跑（Long Run）：以「輕鬆跑配速」進行（與 easy 同區間，不得比 easy 快），距離不超過約 16 英里（約 26 km）；靠累積疲勞而非距離。
嚴格由慢到快：長跑 ≈ 輕鬆跑 ＞ Tempo(＝MP) ＞ 力量課 ＞ 速度課。
必須避免的常見錯誤：把 Tempo 設得比 MP 快；或把長跑配速設得比輕鬆跑還快。""",
    "lydiard": """【Lydiard — 配速區間定義（有氧基礎優先，多依感覺/心率而非固定配速公式）】
* 有氧/基礎跑（Aerobic base）：舒適有氧強度，約 MP + 0:60 ～ 1:45/km，明顯低於無氧閾值。70-80% HRmax。基礎期的主體。
* 穩定跑/有氧閾值（Steady）：接近但低於閾值，約 MP + 0:10 ～ 0:30/km。
* 山坡期（Hill phase）：以山坡跳躍/彈跳練力量，依強度而非公路配速。
* 無氧/田徑期（後段）：比 MP 快的間歇與計時跑，做最後銳化。
* 長跑（Long Run）：有氧強度，與輕鬆跑同區間（不得比 easy 快）。
嚴格由慢到快：長跑 ≈ 有氧基礎 ＞ 穩定跑 ＞ MP ＞ 田徑間歇。
基礎期以有氧發展為優先，不要在基礎期就開很快的配速。""",
}


# ── 課表結構化輸出指示（附加到 prompt 結尾，供前端畫 7 天課表卡片）──
# AI 在 markdown 課表之外，額外輸出一段 ```json 區塊，schema 如下。
SCHEDULE_JSON_INSTRUCTION_ZH = """

【★額外輸出：下週課表的結構化 JSON（供程式繪製 7 天課表卡片）】
在你上面的完整文字分析與課表「之後」，請「務必」再附上一段用 ```json 包起來的程式可解析資料，
內容為下週 7 天（週一到週日）的課表陣列。嚴格遵守下列格式，不要加註解、不要改欄位名：

```json
{
  "weekly_schedule": [
    {"day": "週一", "type": "easy", "distance_km": 10, "pace": "5:30", "hr": "140-150", "description": "輕鬆有氧"},
    {"day": "週二", "type": "interval", "distance_km": 12, "pace": "3:50", "hr": "180-190", "description": "1000m×5，組間慢跑400m"},
    {"day": "週三", "type": "rest", "distance_km": 0, "pace": "", "hr": "", "description": "休息"}
  ]
}
```

欄位說明：
* day：週一～週日（7 天都要有，依序）。
* type：只能是 easy / tempo / interval / long / rest 其中之一（對應輕鬆跑/節奏跑/間歇跑/長跑/休息）。
* distance_km：數字（休息日填 0）。
* pace：字串如 "5:30"（每公里，休息日填 ""）。
* hr：目標心率範圍字串如 "140-150"（可留 ""）。
* description：一句話說明該日課表重點。
這段 JSON 必須與上面文字課表的內容一致。"""

SCHEDULE_JSON_INSTRUCTION_EN = """

[* Extra output: next week's plan as structured JSON (for rendering a 7-day plan)]
AFTER your full text analysis and plan above, you MUST also append a machine-parsable block
wrapped in ```json, containing the next 7 days (Mon-Sun). Follow the format strictly; do not
add comments or rename fields:

```json
{
  "weekly_schedule": [
    {"day": "Mon", "type": "easy", "distance_km": 10, "pace": "5:30", "hr": "140-150", "description": "Easy aerobic"},
    {"day": "Tue", "type": "interval", "distance_km": 12, "pace": "3:50", "hr": "180-190", "description": "1000m x5, 400m jog recovery"},
    {"day": "Wed", "type": "rest", "distance_km": 0, "pace": "", "hr": "", "description": "Rest"}
  ]
}
```

Field rules:
* day: Mon..Sun (all 7 days, in order).
* type: one of easy / tempo / interval / long / rest ONLY.
* distance_km: number (0 for rest).
* pace: string like "5:30" per km ("" for rest).
* hr: target HR range string like "140-150" (may be "").
* description: one short sentence for the day.
The JSON must match the text plan above."""


def normalize_terms(text):
    """對 AI 輸出做確定性術語正規化，兜底修正模型未遵守術語規範的情況。
    - 易感跑/易感帶/易感 → 輕鬆跑
    - 數值範圍的半形波浪號 ~ → 全形 ～（避免 marked.js 的 GFM 刪除線把 ~a~b~ 劃線）
    """
    if not text:
        return text
    # 先處理較長的詞，避免「易感跑」被先換成「輕鬆跑跑」
    text = text.replace("易感跑", "輕鬆跑")
    text = text.replace("易感帶", "輕鬆跑配速帶")
    text = text.replace("易感", "輕鬆")
    # 表示範圍的波浪號（被數字/字母/冒號/百分號夾住）轉全形，避免觸發刪除線
    text = _re.sub(r'(?<=[0-9A-Za-z:%）)])\s*~\s*(?=[0-9A-Za-z:%（(])', '～', text)
    return text


_VALID_TYPES = {"easy", "tempo", "interval", "long", "rest"}


def extract_schedule(raw_text):
    """從 AI 原始回應中解析 ```json 區塊內的 weekly_schedule。
    回傳正規化後的 7 天課表 list，解析失敗回傳 None。
    必須在 content 的 LaTeX 清理（會刪除 '}'）之前呼叫。"""
    if not raw_text:
        return None
    import json as _json
    # 擷取所有 ```json ... ``` 區塊，取第一個含 weekly_schedule 的
    for m in _re.finditer(r"```json\s*(.*?)```", raw_text, _re.S):
        block = m.group(1).strip()
        try:
            data = _json.loads(block)
        except Exception:
            continue
        sched = data.get("weekly_schedule") if isinstance(data, dict) else None
        if not isinstance(sched, list) or not sched:
            continue
        out = []
        for d in sched:
            if not isinstance(d, dict):
                continue
            t = str(d.get("type", "")).strip().lower()
            if t not in _VALID_TYPES:
                t = "easy"
            try:
                dist = float(d.get("distance_km") or 0)
            except (TypeError, ValueError):
                dist = 0.0
            out.append({
                "day": str(d.get("day", "")).strip(),
                "type": t,
                "distance_km": round(dist, 1),
                "pace": str(d.get("pace", "") or "").strip(),
                "hr": str(d.get("hr", "") or "").strip(),
                "description": str(d.get("description", "") or "").strip(),
            })
        if out:
            return out
    return None


def strip_schedule_json(text):
    """移除 markdown 中的 ```json ... ``` 區塊（避免使用者看到原始 JSON）。"""
    if not text:
        return text
    return _re.sub(r"```json\s*.*?```\s*", "", text, flags=_re.S).rstrip()
