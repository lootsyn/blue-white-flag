# 🟦⬜ 청기백기 휴머노이드 (MuJoCo 4 + jev)

MuJoCo 4 기반 휴머노이드 로봇이 양손에 청기(오른손, 파랑)와 백기(왼손, 흰색)를 들고
**텍스트 또는 음성**으로 진행되는 청기백기 게임을 수행합니다.

명령 해석은 **jev** (TypeSafe의 System One 구조화 결정 모델)이 담당하며,
jev 키가 없어도 **opencode CLI** 또는 **내장 한국어 규칙 파서**로 즉시 플레이할 수 있습니다.

---

## 빠른 시작

```bash
# 1) 가상환경 + 의존성 설치
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 웹으로 게임 (권장 — 화면 + 텍스트/음성 입력을 브라우저에서)
.venv/bin/python web_server.py
# → 브라우저에서 http://localhost:8000 접속

# 3) 데스크톱 뷰어로 게임 (터미널 입력)
.venv/bin/python main.py --text        # 텍스트 명령
.venv/bin/python main.py --voice       # 서버 마이크로 음성 (Enter 후 말하기)
.venv/bin/python main.py --demo        # 자동 데모
.venv/bin/python main.py --demo --headless --rounds 8   # 뷰어 없는 CI/서버 데모
```

> **뷰어(3D 화면)**는 GUI 환경에서 실행됩니다. 화면 없는 서버에서는
> 웹 모드(`web_server.py` — 서버에서 렌더해 브라우저로 스트리밍)를 쓰거나
> `MUJOCO_GL=egl .venv/bin/python render_preview.py`로 프리뷰 이미지를 확인하세요.

---

## 🌐 웹 모드 (web_server.py) — 화면 + 텍스트/음성을 브라우저에서

```bash
.venv/bin/python web_server.py                    # http://localhost:8000
.venv/bin/python web_server.py --port 8000 --fps 25 --width 960 --height 540
```

- **화면**: 서버가 MuJoCo를 오프스크린 렌더(MUJOCO_GL=egl) → JPEG → WebSocket으로 실시간 스트리밍 (이 서버 기준 ~20fps)
- **텍스트**: 페이지의 입력창에 명령 입력
- **음성** 🎙: 브라우저의 **Web Speech API(ko-KR)**로 인식 — Chrome/Edge 필요
- **실시간 번복 반영** ⚡: `interimResults`로 말하는 **도중의 중간 결과**를 서버로 즉시 보냅니다.
  "청기 올려… 잠깐, 내려!"라고 말하면 "올려"가 반영된 로봇이 **현재 동작을 즉시 갈아타고** 내립니다.
  이미 같은 동작 중이면 서버가 의미적 중복(dedup)을 걸러 재실행하지 않습니다.

### 원격 서버에서 접속 (SSH 터널)

```bash
ssh -L 8000:localhost:8000 user@서버    # 이후 로컬 브라우저에서 http://localhost:8000
```

> 음성 인식은 브라우저에서 수행되므로, 터널로 `localhost`에 접속하면 Web Speech API
> 권한 문제 없이 사용할 수 있습니다 (HTTPS가 아닌 직접 IP 접속은 브라우저 정책상
> 마이크가 차단될 수 있어요).

### 웹 통합 테스트

```bash
.venv/bin/python test_web.py    # 서버 자동 실행 → 스트림/명령/번복/음성 흐름/deny 검증
```

---

## 게임 방법

터미널(또는 마이크)에 진행자처럼 외치면, jev가 명령을 해석해 로봇이 즉시 수행합니다.

| 말 | 로봇 동작 |
| --- | --- |
| `청기 올려` | 🟦 오른팔을 머리 위로 빠르게! |
| `백기 내려` | ⬜ 왼팔을 아래로 |
| `청기 올리지 마` / `백기 내리지 마` | 절대 움직이지 않음 (😎) |
| `손 대지 마` / `가만히 있어` | 양깃발 모두 그대로 |
| `청기백기 올려` / `다 내려` / `손 들어` | 양쪽 동시에 |
| `청기 올려 놓고 백기 내려` | 조합 명령도 순서대로 |
| (알아들을 수 없는 말) | 🤷 팔꿈치를 펄럭이며 "무슨 말인지 모르겠어요!" |

로봇은 사람처럼 **팔꿈치를 먼저 접고 어깨로 스윙**해 0.5초 안에 깃발을 올리고,
깃발 천은 살랑살랑 펄럭이며, 동작 후에는 몸의 기울기를 스스로 원위치합니다.

---

## jev 설정 (API 키)

이 프로젝트의 명령 파서는 3중 엔진으로 동작합니다 (`--engine` 옵션).

| 엔진 | 설명 | 키 필요 | 선택 방법 |
| --- | --- | --- | --- |
| **jev** | TypeSafe System One (`POST https://api.typesafe.ai/v1/systemone`) — 청기/백기 동작을 `choice` 질문 2개, 명령 유효성을 `noul` 질문 1개로 물어 구조화된 답을 받습니다. 호출당 ~0.2초 | ⭕ | 키가 있으면 자동 |
| **opencode** | 로컬 `opencode` CLI (opencode-go 무료 모델) | ❌ | jev 키가 없을 때 자동 |
| **offline** | 내장 한국어 규칙 파서 (정규식) | ❌ | 위 둘이 모두 실패할 때 |

### 키 설정 방법 (jev 엔진 활성화)

**방법 A — 설정 파일** (권장): 프로젝트 폴더에 `jev.json` 생성
(`jev.json.example` 참고. 이 파일은 `.gitignore`에 포함되어 있어 커밋되지 않습니다):

```json
{
  "base_url": "https://api.typesafe.ai",
  "api_key": "apikey_여기에_키",
  "model": "jev-latest"
}
```

**방법 B — 환경변수**:

```bash
export JEV_API_KEY="apikey_..."        # 또는 TYPESAFE_API_KEY
export JEV_BASE_URL="https://api.typesafe.ai"   # (기본값)
export JEV_MODEL="jev-latest"                    # (기본값)
```

설정 후 실행하면 시작 배너에서 확인할 수 있습니다:

```
🧠 명령 파서: jev (TypeSafe System One) ✅  endpoint=https://api.typesafe.ai  model=jev-latest
```

> 참고: opencode-go 프로바이더에는 'jev'라는 이름의 모델이 없는 것을 확인했습니다.
> jev는 TypeSafe(https://docs.typesafe.ai)의 별개 서비스이며 OpenAI 호환 API가 아닙니다.
> 그래서 jev 엔진은 전용 어댑터(`parser.py`의 `parse_with_jev`)로 `/v1/systemone`을 호출합니다.
> 키가 없는 환경에서도 게임이 막히지 않도록 opencode/offline 폴백 체인이 자동 작동합니다.

---

## 음성 입력 (`--voice`)

- `sounddevice`로 마이크 녹음 → `SpeechRecognition`의 Google Web Speech(`ko-KR`)로 인식합니다.
- **Enter를 누르면 최대 4초간 듣습니다.** 인터넷 연결이 필요합니다.
- 마이크가 없는 서버에서는 안내 메시지와 함께 종료됩니다 → `--text` 모드를 사용하세요.

---

## 프로젝트 구조

| 파일 | 역할 |
| --- | --- |
| `humanoid_flags.xml` | MJCF 휴머노이드 모델: 자유관절 골반, 16개 위치 서보 관절, 오른손 청기/왼손 백기(깃대+천, 힌지로 펄럭임) |
| `robot.py` | 로봇 컨트롤러: 최소저크 궤적 엔진, '팔꿈치 먼저' 동작 시퀀스, 자세 트림(발목 전략) 균형 유지 |
| `parser.py` | 명령 파서: jev(TypeSafe System One) / opencode CLI / offline 규칙 파서 3중 엔진 |
| `voice.py` | 음성 입력(STT): 마이크 녹음 + Google Web Speech 한국어 인식 |
| `main.py` | 데스크톱 게임 루프: MuJoCo 뷰어 + 입력 스레드(텍스트/음성/데모) + 명령 큐 |
| `web_server.py` | 웹 서버: FastAPI + WebSocket — 화면 JPEG 스트리밍 + 텍스트/음성 명령 (번복 즉시 반영) |
| `web/index.html` | 웹 UI: 스트림 캔버스, 실시간 음성(Web Speech API interim), 상태 칩, 명령 기록 |
| `render_preview.py` | 오프스크린 렌더 프리뷰 (`MUJOCO_GL=egl` 등) → `preview/*.png` |
| `test_headless.py` | 헤드리스 물리 테스트: 서있기·스냅 타임·균형·안정성 자동 검증 |
| `test_web.py` | 웹 통합 테스트: 스트림 FPS·명령 반영·급반전·음성 interim 흐름 자동 검증 |
| `jev.json.example` | jev 키 설정 파일 템플릿 |

---

## 로봇 동작 튜닝

"사람처럼 빠릿한" 동작의 핵심 파라미터 (`robot.py` 상단):

| 파라미터 | 값 | 의미 |
| --- | --- | --- |
| `T_FOLD_UP` / `T_SWING_UP` | 0.16 / 0.32 s | 팔꿈치 접기 → 어깨 스윙 (합 0.48s 만에 깃발 머리 위) |
| `T_SWING_DOWN` / `T_FOLD_DOWN` | 0.32 / 0.16 s | 내리기 시퀀스 |
| `TRIM_KP / TRIM_KD / TRIM_TAU` | 0.5 / 0.35 / 0.25 s | 몸 기울기 자동 복원(발목 전략), 저역통과 필터로 발목 '편�래필래' 방지 |

물리 게인은 `humanoid_flags.xml`의 `<actuator>`에서 조정합니다.
(발목 kp=1500: 역진자 안정화 임계값 mgh≈470 Nm/rad보다 충분히 크게 유지 — 핵심!)

### 테스트

```bash
.venv/bin/python test_headless.py   # 서있기/스냅/균형/안정성 자동 검증
```

측정 결과 (현재 설정):
- 서있기 안정화 후 몸 기울기: **±0.1° 이내**
- 청기 올리기 95% 도달: **0.47초**
- 전체 명령 시퀀스 중 최대 기울기: **≤9°** (사람이 팔 흔들 때 흔들리는 정도) 후 자동 복원
- 최종 health (서있음 점수): **0.99+**

---

## 문제 해결

| 증상 | 해결 |
| --- | --- |
| 웹 화면이 안 나와요 (뷰어만) | 서버에 GL이 없으면 명령은 동작. `MUJOCO_GL=osmesa` 시도 또는 EGL 설치 |
| 웹 음성 버튼이 안 돼요 | Chrome/Edge 사용, 마이크 권한 허용. HTTPS가 아니면 `localhost` 또는 SSH 터널로 접속 |
| 웹 화면이 버벅여요 | `--fps 15 --width 800 --height 450` 로 낮추기 (소프트웨어 GL 환경) |
| 데스크톱 뷰어가 안 열려요 (서버) | 웹 모드 사용, 또는 `MUJOCO_GL=egl python render_preview.py` |
| `ModuleNotFoundError: mujoco` | `.venv/bin/pip install -r requirements.txt` |
| 마이크 인식이 안 돼요 (데스크톱 --voice) | 인터넷 연결 확인, 마이크 권한 확인, `--text` 모드 사용 |
| jev 호출 실패 | 키(`jev.json`) 확인 — 실패 시 자동으로 opencode/offline으로 전환되어 게임은 계속됩니다 |
| 음성을 못 알아들어요 | 좀 더 또박또박, 짧게 말하세요 (예: "청기 올려") |
