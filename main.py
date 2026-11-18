#!/usr/bin/env python3
"""청기백기 게임 — MuJoCo 4 휴머노이드 + jev 명령 파서.

사용법:
  python main.py                 # 텍스트 명령 (기본)
  python main.py --voice         # 음성 명령 (Enter → 말하기)
  python main.py --demo           # 자동 진행 데모
  python main.py --demo --headless   # 뷰어 없이 데모 (CI/서버)
  python main.py --engine offline # 파서 엔진 강제 지정 (jev|opencode|offline)

로봇: 오른손 🟦 청기 / 왼손 ⬜ 백기
"""
from __future__ import annotations

import argparse
import os
import queue
import random
import sys
import threading
import time

from robot import FlagRobot
from parser import CommandParser
import voice as voice_mod

# ----------------------------------------------------------------------------
# 데모 명령 풀
# ----------------------------------------------------------------------------

DEMO_COMMANDS = [
    "청기 올려", "백기 올려", "청기 내려", "백기 내려",
    "청기 올리지 마", "백기 내리지 마", "손 대지 마",
    "청기백기 올려", "다 내려", "양손 들어",
    "청기 올려 놓고 백기 내려", "백기 올려 놓고 청기 내려",
    "가만히 있어", "청기!",
]

BANNER = r"""
==========================================================
   🟦⬜  청 기 백 기  (MuJoCo 4 Humanoid)  🟦⬜
==========================================================
  로봇: 오른손=청기(파랑) / 왼손=백기(흰색)
  명령 예시:
    청기 올려 / 백기 올려 / 청기 내려 / 백기 내려
    청기 올리지 마 / 백기 내리지 마 / 손 대지 마
    청기백기 올려 / 다 내려 / 손 들어
    청기 올려 놓고 백기 내려 (조합 가능)
  종료: 뷰어 창 닫기 또는 Ctrl+C
==========================================================
"""


def format_state(robot: FlagRobot) -> str:
    up = robot.flags_up()
    blue = "🟦 올림" if up["blue"] else "🟦 내림"
    white = "⬜ 올림" if up["white"] else "⬜ 내림"
    phi, psi = robot.pelvis_tilt_deg
    return f"{blue} | {white}  (몸 기울기 {phi:+.1f}°)"


def execute(robot: FlagRobot, parsed: dict) -> None:
    """파싱된 명령을 로봇에 적용.

    게임 규칙: '올려'는 올렸다가 자동으로 내려오는 게스처, '내려'는 즉시 번복,
    '가만히/올리지 마'는 현재 자리에서 얼어붙기.
    """
    if parsed.get("unknown") and not parsed.get("actions"):
        robot.deny()
        return
    if not parsed.get("actions"):
        # 가만히 명령 ("손 대지 마", "가만히" 등) — 그 자리에서 정지
        robot.freeze("both")
        return
    for action in parsed["actions"]:
        if action["dir"] in ("up", "down"):
            robot.command(action["flag"], action["dir"])
        elif action["dir"] == "hold":
            robot.freeze(action["flag"])


# ----------------------------------------------------------------------------
# 입력 워커 (별도 스레드)
# ----------------------------------------------------------------------------


def make_worker(mode: str, parser_: CommandParser, cmd_q: queue.Queue,
                rounds: int, done_event: threading.Event):
    def text_worker():
        while True:
            try:
                raw = input("📣 명령 > ").strip()
            except (EOFError, KeyboardInterrupt):
                done_event.set()
                return
            if not raw:
                continue
            parsed = parser_.parse(raw)
            print(f"   🤖 {parsed['reply']}   [{parsed['engine']}]")
            cmd_q.put(parsed)

    def voice_worker():
        vi = voice_mod.VoiceInput()
        problem = vi.self_test()
        if problem:
            print(f"\n⚠️ 마이크 사용 불가: {problem}")
            print("   → 텍스트 모드로 실행하세요: python main.py --text\n")
            done_event.set()
            return
        while True:
            try:
                input("\n⏎ Enter를 누르고 말하세요… ")
            except (EOFError, KeyboardInterrupt):
                done_event.set()
                return
            try:
                text = vi.listen()
            except voice_mod.VoiceError as e:
                print(f"   ⚠️ {e}")
                continue
            print(f"   🎧 들었어요: {text!r}")
            parsed = parser_.parse(text)
            print(f"   🤖 {parsed['reply']}   [{parsed['engine']}]")
            cmd_q.put(parsed)

    def demo_worker():
        commands = [random.choice(DEMO_COMMANDS) for _ in range(max(1, rounds))]
        for i, cmd in enumerate(commands, 1):
            print(f"\n── 라운드 {i}/{len(commands)} ──  📣 {cmd}")
            parsed = parser_.parse(cmd)
            print(f"   🤖 {parsed['reply']}   [{parsed['engine']}]")
            cmd_q.put(parsed)
            # 진행자 호흡 (로봇 동작 완료 후 다음 명령)
            time.sleep(random.uniform(1.0, 1.8))
        done_event.set()

    return {
        "text": text_worker,
        "voice": voice_worker,
        "demo": demo_worker,
    }[mode]


# ----------------------------------------------------------------------------
# 게임 루프
# ----------------------------------------------------------------------------


def run_viewer(robot: FlagRobot, cmd_q: queue.Queue, done_event: threading.Event):
    import mujoco
    import mujoco.viewer

    with mujoco.viewer.launch_passive(robot.model, robot.data) as viewer:
        # 초기 카메라: 상반신 + 든 깃발 클로즈업 (마우스로 자유롭게 조작 가능)
        cam = viewer.cam
        cam.lookat[:] = [0.0, 0.0, 1.45]
        cam.distance = 3.2
        cam.azimuth = 18
        cam.elevation = 3

        print("\n뷰어 실행 중! (마우스 드래그=회전, 휠=줌)\n")
        robot.settle(2.0)
        print(f"🤖 {robot.salute()}")
        viewer.sync()

        last_wall = time.perf_counter()
        try:
            while viewer.is_running():
                # 새 명령 소비: 대기 중 명령이 여러 개면 '가장 최근 것'만 실행 (반응성!)
                latest = None
                while True:
                    try:
                        latest = cmd_q.get_nowait()
                    except queue.Empty:
                        break
                if latest is not None:
                    execute(robot, latest)

                # 16ms 물리 + 렌더 + 실시간 페이싱
                for _ in range(16):
                    robot.step()
                viewer.sync()
                now = time.perf_counter()
                if now - last_wall < 0.016:
                    time.sleep(0.016 - (now - last_wall))
                    now = time.perf_counter()
                last_wall = now
        finally:
            pass
    print("\n뷰어 종료 — 게임 끝!")


def run_headless(robot: FlagRobot, cmd_q: queue.Queue, done_event: threading.Event,
                 timeout_s: float = 180.0):
    print("\n헤드리스 모드 (뷰어 없음) — 데모를 수행합니다.")
    robot.settle(1.5)
    print(f"🤖 {robot.salute()}")

    start = time.perf_counter()
    was_busy = False
    while True:
        latest = None
        while True:
            try:
                latest = cmd_q.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            execute(robot, latest)
            was_busy = True

        robot.step()

        now = time.perf_counter()
        if now - start > timeout_s:
            print("타임아웃 — 종료")
            break
        # 동작이 '끝난 순간'에만 상태 출력 (스팸 방지)
        if was_busy and robot.busy() <= 0:
            print(f"   {format_state(robot)}")
            was_busy = False
        # 데모 완료 조건: 워커 종료 + 큐 비었음 + 로봇 한가함
        if done_event.is_set() and cmd_q.empty() and robot.busy() <= 0:
            break
    print(f"\n최종 상태: {format_state(robot)}  health={robot.standing_health()}")
    print("헤드리스 데모 완료!")


# ----------------------------------------------------------------------------
# 진입점
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="청기백기 MuJoCo 휴머노이드 게임")
    ap.add_argument("--mode", choices=["text", "voice", "demo"], default=None,
                    help="입력 방식 (기본: text)")
    ap.add_argument("--text", action="store_true", help="텍스트 명령 모드")
    ap.add_argument("--voice", action="store_true", help="음성 명령 모드")
    ap.add_argument("--demo", action="store_true", help="자동 진행 데모 모드")
    ap.add_argument("--engine", choices=["auto", "jev", "opencode", "offline"],
                    default="auto", help="명령 파서 엔진 (기본: auto)")
    ap.add_argument("--model", default=None,
                    help="엔진 모델 지정 (jev: JEV_MODEL env / opencode: opencode-go/<id>)")
    ap.add_argument("--rounds", type=int, default=10, help="데모 라운드 수")
    ap.add_argument("--headless", action="store_true", help="뷰어 없이 실행 (데모/CI용)")
    args = ap.parse_args()

    mode = args.mode or ("demo" if args.headless else None)
    if mode is None:
        mode = "demo" if args.demo else ("voice" if args.voice else "text")

    print(BANNER)

    parser_ = CommandParser(engine=args.engine, model=args.model)
    print(f"🧠 명령 파서: {parser_.describe()}\n")

    robot = FlagRobot()
    cmd_q: queue.Queue = queue.Queue()
    done_event = threading.Event()

    worker = make_worker(mode, parser_, cmd_q, args.rounds, done_event)
    threading.Thread(target=worker, daemon=True).start()

    try:
        if args.headless:
            run_headless(robot, cmd_q, done_event)
        else:
            run_viewer(robot, cmd_q, done_event)
    except KeyboardInterrupt:
        print("\n게임 종료!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
