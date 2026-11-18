#!/usr/bin/env python3
"""청기백기 명령 파서 — 3중 엔진.

엔진 우선순위 (auto 모드):
  1. jev        : OpenAI 호환 API (JEV_BASE_URL/JEV_API_KEY/JEV_MODEL 또는 jev.json)
  2. opencode   : 로컬 opencode CLI (opencode-go 무료 모델 등, API 키 불필요)
  3. offline    : 내장 한국어 규칙 파서 (키·네트워크 불필요, 항상 사용 가능)

출력 스키마 (모든 엔진 동일):
  {
    "reply":   "로봇이 외칠 짧은 한국어 멘트",
    "actions": [{"flag": "blue|white|both|none", "dir": "up|down|hold"}],
    "unknown": bool,   # True면 로봇이 '모르겠다' 동작
    "engine":  "사용된 엔진 이름"
  }

※ 조사 결과 opencode-go 프로바이더에는 'jev'라는 이름의 모델이 없음
  → 요청대로 API 키 기반 구성(JEV_*)을 기본으로 제공하고,
    키가 없어도 게임이 즉시 플레이 가능하도록 폴백 체인을 구성함.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

VALID_FLAGS = ("blue", "white", "both", "none")
VALID_DIRS = ("up", "down", "hold")

# ----------------------------------------------------------------------------
# jev (OpenAI 호환 엔드포인트) 설정 로드
# ----------------------------------------------------------------------------


def load_jev_config() -> dict | None:
    """env(JEV_*/TYPESAFE_API_KEY) → jev.json 순서로 설정 탐색.

    jev는 TypeSafe의 System One 결정 모델이다 (OpenAI 호환 API가 아님).
    base_url 기본값: https://api.typesafe.ai  (OpenRouter 등 호환 엔드포인트로 변경 가능)
    """
    base_url = os.environ.get("JEV_BASE_URL", JEV_DEFAULT_BASE_URL)
    model = os.environ.get("JEV_MODEL", JEV_DEFAULT_MODEL)
    api_key = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")

    if not api_key:
        # 프로젝트 디렉토리 → 사용자 설정 디렉토리
        for path in (
            os.path.join(HERE, "jev.json"),
            os.path.expanduser("~/.config/flaggame/jev.json"),
        ):
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    base_url = data.get("base_url") or base_url
                    model = data.get("model") or model
                    api_key = data.get("api_key")
                    break
                except Exception:
                    pass
    if not api_key:
        return None
    return {"base_url": base_url, "api_key": api_key, "model": model}


# ----------------------------------------------------------------------------
# LLM 프롬프트 (jev / opencode 공용)
# ----------------------------------------------------------------------------

SYSTEM_PROMPT = """너는 '청기백기' 게임 로봇의 명령 해석기다. 진행자의 한국어 말을 로봇 동작 JSON으로 바꿔라.

로봇: 오른손=청기(파랑), 왼손=백기(흰색).
출력은 JSON 하나만, 다른 설명 금지:
{"reply":"로봇이 외칠 짧고 신나는 한국어 멘트","actions":[{"flag":"blue|white|both|none","dir":"up|down|hold"}],"unknown":false}

규칙:
- "올려/올리/들어/올라" → up, "내려/내리" → down
- 부정 명령("올리지 마", "내리지 마", "하지 마", "손 대지 마", "가만히", "멈춰") → 깃발을 언급했으면 그 깃발 dir="hold", 언급 안 했으면 actions=[] 에 unknown=false (로봇이 가만히 있음)
- "청기백기", "양손", "둘 다", "다", "모두" → flag="both"
- 한 문장에 여러 명령이면 actions에 순서대로 나열
- 도저히 알아들을 수 없는 말이면 unknown=true, actions=[]

예시:
"청기 올려" → {"reply":"청기 올려!","actions":[{"flag":"blue","dir":"up"}],"unknown":false}
"백기 내리지 마" → {"reply":"백기는 그대로!","actions":[{"flag":"white","dir":"hold"}],"unknown":false}
"청기 올려 놓고 백기 내려" → {"reply":"청기 올리고, 백기 내려!","actions":[{"flag":"blue","dir":"up"},{"flag":"white","dir":"down"}],"unknown":false}
"손 대지 마" → {"reply":"안 움직여요!","actions":[],"unknown":false}
"피자 먹고 싶다" → {"reply":"그건 깃발 명령이 아니잖아요?!","actions":[],"unknown":true}"""


def _validate_llm_json(raw: str) -> dict | None:
    """LLM 응답에서 JSON 추출·검증. 실패 시 None."""
    if not raw:
        return None
    # 코드펜스/잡문 제거 후 첫 JSON 객체 파싱
    text = re.sub(r"```(?:json)?|```", "", raw)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return None
    actions = []
    for a in data.get("actions", []):
        if not isinstance(a, dict):
            continue
        flag = str(a.get("flag", "none")).lower().strip()
        direction = str(a.get("dir", "hold")).lower().strip()
        if flag not in VALID_FLAGS:
            flag = "none"
        if direction not in VALID_DIRS:
            direction = "hold"
        if flag == "none" and direction == "hold":
            continue  # no-op
        actions.append({"flag": flag, "dir": direction})
    unknown = bool(data.get("unknown", not actions and not data.get("reply")))
    reply = str(data.get("reply", "")).strip()[:80]
    return {"reply": reply, "actions": actions, "unknown": unknown}


# ----------------------------------------------------------------------------
# 엔진 1: jev — TypeSafe System One (POST {base}/v1/systemone)
#   jev는 채팅 모델이 아니라 '타입화된 질문→구조화된 답' 결정 모델.
#   청기/백기 동작을 choice 질문 2개, 명령 유효성을 noul 질문 1개로 물어본다.
#   참고: https://docs.typesafe.ai  (엔드포인트 POST api.typesafe.ai/v1/systemone)
# ----------------------------------------------------------------------------

JEV_DEFAULT_BASE_URL = "https://api.typesafe.ai"
JEV_DEFAULT_MODEL = "jev-latest"

JEV_STATE_TEMPLATE = """게임: 청기백기 (한국의 전통 깃발 게임).
로봇: 오른손에 청기(파란 깃발), 왼손에 백기(흰 깃발)를 들고 있다.
규칙:
- "올려/올리/들어/올라" 는 해당 깃발을 머리 위로 올리는 명령이다.
- "내려/내리" 는 해당 깃발을 아래로 내리는 명령이다.
- "올리지 마/내리지 마/하지 마/손 대지 마/가만히/멈춰" 같은 부정 표현이 붙으면,
  언급된 깃발(언급이 없으면 모든 깃발)을 절대 움직이면 안 된다.
- "청기백기/양손/둘 다/다/모두/싹 다" 같은 표현은 두 깃발 모두를 가리킨다.
- 한 문장에 여러 명령이 조합될 수 있다 (예: "청기 올려 놓고 백기 내려").
- 깃발 색 언급 없이 "손 들어/손 내려" 는 두 깃발 모두에 적용된다.
진행자가 외친 말: "{heard}"
"""

_JEV_QUESTIONS = {
    "blue_action": {
        "type": "choice",
        "instructions": "청기(오른손의 파란 깃발)가 해야 할 동작 하나를 고르라.",
        "criteria": {
            "up": "청기를 올리라는 긍정 명령이 state에 있다.",
            "down": "청기를 내리라는 긍정 명령이 state에 있다.",
            "hold": "청기를 '올리지 마/내리지 마' 등의 부정 명령으로 언급했거나, 청기를 전혀 언급하지 않았다.",
        },
    },
    "white_action": {
        "type": "choice",
        "instructions": "백기(왼손의 흰 깃발)가 해야 할 동작 하나를 고르라.",
        "criteria": {
            "up": "백기를 올리라는 긍정 명령이 state에 있다.",
            "down": "백기를 내리라는 긍정 명령이 state에 있다.",
            "hold": "백기를 '올리지 마/내리지 마' 등의 부정 명령으로 언급했거나, 백기를 전혀 언급하지 않았다.",
        },
    },
    "is_command": {
        "type": "noul",
        "instructions": "진행자의 말은 청기백기 게임에서 로봇이 수행할 수 있는 유효한 명령인가?",
        "criteria": {
            "true": "깃발 올리기/내리기/움직이지 말기 명령(부정 명령 포함)이다.",
            "false": "게임과 무관한 잡담이거나 알아들을 수 없는 말이다.",
        },
    },
}

# 애매하면 안 움직이는 것이 안전 (jev confidence 낮으면 hold)
_JEV_MIN_CONF = 0.25
_JEV_MIN_NOUL = 0.5


def _jev_reply(heard: str, blue: str, white: str, is_cmd: bool) -> str:
    if not is_cmd:
        return "무슨 말인지 모르겠어요!"
    parts = []
    mentions_blue = bool(re.search(r"청|파란|파랑|블루", heard))
    mentions_white = bool(re.search(r"백|흰|하얀|화이트", heard))
    if blue == "up":
        parts.append("청기 올려!")
    elif blue == "down":
        parts.append("청기 내려!")
    elif blue == "hold" and mentions_blue:
        parts.append("청기는 그대로!")
    if white == "up":
        parts.append("백기 올려!")
    elif white == "down":
        parts.append("백기 내려!")
    elif white == "hold" and mentions_white:
        parts.append("백기는 그대로!")
    if not parts:
        return "😎 안 움직여요!"
    return " ".join(parts)


def parse_with_jev(text: str, cfg: dict) -> dict | None:
    try:
        import requests
    except ImportError:
        return None
    url = cfg["base_url"].rstrip("/") + "/v1/systemone"
    payload = {
        "model": cfg["model"],
        "state": JEV_STATE_TEMPLATE.format(heard=text.strip() or "(빈 말)"),
        "questions": _JEV_QUESTIONS,
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        answers = data.get("answers", {})
        blue_a = answers.get("blue_action", {})
        white_a = answers.get("white_action", {})
        cmd_a = answers.get("is_command", {})

        is_cmd = float(cmd_a.get("noul", 0.0)) >= _JEV_MIN_NOUL
        blue = blue_a.get("choice", "hold")
        white = white_a.get("choice", "hold")
        blue_conf = float(blue_a.get("confidence", 1.0))
        white_conf = float(white_a.get("confidence", 1.0))

        if not is_cmd:
            return {"reply": _jev_reply(text, "hold", "hold", False),
                    "actions": [], "unknown": True}

        # confidence 낮으면 안전하게 hold (로봇이 헛동작하지 않게)
        if blue_conf < _JEV_MIN_CONF:
            blue = "hold"
        if white_conf < _JEV_MIN_CONF:
            white = "hold"

        actions = []
        if blue in ("up", "down"):
            actions.append({"flag": "blue", "dir": blue})
        if white in ("up", "down"):
            actions.append({"flag": "white", "dir": white})
        reply = _jev_reply(text, blue, white, True)
        unknown = False
        hold_all = not actions
        return {"reply": reply, "actions": actions, "unknown": unknown,
                "hold_all": hold_all}
    except Exception as e:
        print(f"   ⚠️ jev 호출 실패({type(e).__name__}: {e}) → 폴백 엔진으로 전환")
        return None


# ----------------------------------------------------------------------------
# 엔진 2: opencode CLI (opencode-go 모델, API 키 불필요)
# ----------------------------------------------------------------------------

DEFAULT_OPENCODE_MODEL = "opencode-go/beam-free"


def opencode_available() -> bool:
    return shutil.which("opencode") is not None


def parse_with_opencode(text: str, model: str | None = None) -> dict | None:
    model = model or os.environ.get("OPENCODE_MODEL", DEFAULT_OPENCODE_MODEL)
    if not opencode_available():
        return None
    cmd = [
        "opencode", "run",
        "--model", model,
        "--format", "json",
        "--archived",
        "--title", "flag-game-parser",
        f"{SYSTEM_PROMPT}\n\n진행자: {text}",
    ]
    # opencode CLI가 백그라운드 서비스(자식 프로세스)를 띄우므로
    # 파이프가 안 닫혀 subprocess.run(timeout=...)이 영구 대기할 수 있음
    # → 새 세션(프로세스 그룹)으로 띄우고 타임아웃 시 그룹 전체를 SIGKILL.
    import signal
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
    except Exception:
        return None
    try:
        out, _ = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            return None

    pieces = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        part = event.get("part", {})
        if event.get("type") == "text" and part.get("type") == "text":
            pieces.append(part.get("text", ""))
    return _validate_llm_json("".join(pieces))


# ----------------------------------------------------------------------------
# 엔진 3: 오프라인 규칙 파서 (항상 사용 가능)
# ----------------------------------------------------------------------------

_R_BLUE = re.compile(r"청|파란|파랑|블루|blue", re.IGNORECASE)
_R_WHITE = re.compile(r"백|흰|하얀|화이트|white", re.IGNORECASE)
_R_BOTH = re.compile(r"청.?백|백.?청|양.?손|양기|둘 ?다|모두|전부|다 ?같이|싹 ?다|막 ?내")
_R_UP = re.compile(r"올리|올려|올라|번쩍|들어 ?라|들어$|들어[^가-힣]|^\s*들어|업\b|raise|up", re.IGNORECASE)
_R_DOWN = re.compile(r"내리|내려|다운|풀어|down|lower", re.IGNORECASE)
_R_NEG = re.compile(r"지\s?마|하지\s?마|가만|멈추|그만|대지|움직이지|참아|잠깐만")
_R_SPLIT = re.compile(r"[,.!?;]|그리고|하고\s|놓고|넣고|갖고|가지고|한 ?다음|다음에|이후에|그 ?후")
_R_HAND = re.compile(r"손|팔")
_R_ALL_DOWNUP = re.compile(r"(?:^|\s)(?:다|모두|전부|죄\s?다|싹)\s*(?=올리|올려|내리|내려|들어|번쩍)")


def _flags_in(clause: str) -> set[str]:
    flags = set()
    if _R_BLUE.search(clause):
        flags.add("blue")
    if _R_WHITE.search(clause):
        flags.add("white")
    if not flags and _R_BOTH.search(clause):
        flags = {"both"}
    elif flags and _R_BOTH.search(clause):
        flags = {"both"}
    return flags


def _dir_in(clause: str) -> str | None:
    if _R_UP.search(clause):
        return "up"
    if _R_DOWN.search(clause):
        return "down"
    return None


def parse_offline(text: str) -> dict:
    text = text.strip()
    if not text:
        return {"reply": "아무 말도 안 들렸어요…", "actions": [], "unknown": True}

    actions = []
    hold_all = False
    clauses = [c for c in _R_SPLIT.split(text) if c and c.strip()]
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        flags = _flags_in(clause)
        direction = _dir_in(clause)
        negative = bool(_R_NEG.search(clause))

        if negative:
            # 부정: 언급된 깃발(들)은 절대 움직이지 않음.
            # 깃발도 손도 언급 안 됨 → 전체 가만히 (hold_all)
            if not flags and not _R_HAND.search(clause):
                hold_all = True
                continue
            targets = flags or {"both"} if _R_HAND.search(clause) else flags
            for f in (targets or {"none"}):
                if f == "none":
                    hold_all = True
                else:
                    actions.append({"flag": f, "dir": "hold"})
            continue

        if direction and not flags and _R_HAND.search(clause):
            flags = {"both"}  # "손 들어" 등
        if direction and not flags and (_R_BOTH.search(clause) or _R_ALL_DOWNUP.search(clause)):
            flags = {"both"}  # "다 내려", "싹 다 내려" 등
        if not direction:
            # "청기!" 처럼 깃발만 외친 경우 → 그 깃발 올리기(게임 관례)
            if flags and flags != {"none"}:
                for f in flags:
                    actions.append({"flag": f, "dir": "up"})
                continue
            continue

        for f in (flags or {"none"}):
            if f == "none":
                continue
            actions.append({"flag": f, "dir": direction})

    if hold_all and not actions:
        return {"reply": "안 움직여요!", "actions": [], "unknown": False, "hold_all": True}

    # no-op 정리
    actions = [a for a in actions if a["flag"] != "none"]

    if not actions:
        return {"reply": "무슨 말인지 모르겠어요!", "actions": [], "unknown": True}

    # 멘트 생성
    names = {"blue": "청기", "white": "백기", "both": "청기·백기", "none": ""}
    parts = []
    for a in actions:
        name = names[a["flag"]]
        verb = {"up": "올려!", "down": "내려!", "hold": "안 움직여요!"}[a["dir"]]
        parts.append((name + " " + verb).strip())
    return {"reply": " ".join(parts), "actions": actions, "unknown": False}


# ----------------------------------------------------------------------------
# 통합 파서
# ----------------------------------------------------------------------------


class CommandParser:
    def __init__(self, engine: str = "auto", model: str | None = None):
        self.engine_mode = engine  # auto | jev | opencode | offline
        self.jev_cfg = load_jev_config()
        self.opencode_model = model
        self._llm_failures = 0     # 연속 실패 카운트 (2회 초과 시 세션 내 offline 강등)
        self._degraded = False

    @property
    def active_engine(self) -> str:
        if self.engine_mode == "offline" or self._degraded:
            return "offline"
        if self.engine_mode == "jev":
            return "jev"
        if self.engine_mode == "opencode":
            return "opencode"
        # auto
        if self.jev_cfg:
            return "jev"
        if opencode_available():
            return "opencode"
        return "offline"

    def describe(self) -> str:
        eng = self.active_engine
        if eng == "jev":
            return (f"jev (TypeSafe System One) ✅  endpoint={self.jev_cfg['base_url']}  "
                    f"model={self.jev_cfg['model']}")
        if eng == "opencode":
            m = self.opencode_model or os.environ.get("OPENCODE_MODEL", DEFAULT_OPENCODE_MODEL)
            return f"opencode CLI ✅  model={m} (API 키 불필요)"
        return ("offline 내장 파서 (키 없음 — JEV_API_KEY/TYPESAFE_API_KEY 또는 jev.json 설정 시 jev 엔진 활성화)")

    def parse(self, text: str) -> dict:
        eng = self.active_engine
        result = None
        used = eng
        if eng == "jev":
            result = parse_with_jev(text, self.jev_cfg)
            if result is None:
                used = "opencode" if opencode_available() else "offline"
                result = parse_with_opencode(text, self.opencode_model) if used == "opencode" else None
        elif eng == "opencode":
            result = parse_with_opencode(text, self.opencode_model)
            if result is None:
                used = "offline"

        # LLM 실패 누적 → 세션 내 offline 강등 (게임이 멈추지 않게)
        if eng != "offline":
            if result is None:
                self._llm_failures += 1
                if self._llm_failures >= 2 and not self._degraded:
                    self._degraded = True
                    print("   ⚠️ LLM 엔진이 계속 실패 — 이번 세션은 offline 파서로 진행합니다")
            else:
                self._llm_failures = 0

        if result is None:
            result = parse_offline(text)
            used = "offline"
        # 스키마 통일
        if "unknown" not in result:
            result["unknown"] = not result.get("actions")
        result.setdefault("hold_all", False)
        if not result.get("reply"):
            fallback = parse_offline(text)
            result["reply"] = fallback["reply"]
            if not result["actions"]:
                result["actions"] = fallback["actions"]
                result["unknown"] = fallback["unknown"]
                result["hold_all"] = fallback.get("hold_all", False)
        result["engine"] = used
        return result


if __name__ == "__main__":
    # 간단 자체 테스트
    p = CommandParser()
    print("엔진:", p.describe())
    for cmd in [
        "청기 올려", "백기 내려", "청기 올리지 마", "손 대지 마", "가만히 있어",
        "청기 올려 놓고 백기 내려", "청기백기 올려", "다 내려",
        "백기 내리지 말고 청기만 올려", "배고파",
    ]:
        print(f"{cmd!r:34s} → {json.dumps(p.parse(cmd), ensure_ascii=False)}")
