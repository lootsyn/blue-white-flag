#!/usr/bin/env python3
"""청기백기 웹 서버 통합 테스트.

순서:
  1. web_server.py 를 테스트 포트에서 실행 (자체 관리)
  2. HTTP /health 확인
  3. WS 접속 → JPEG 프레임 스트림 FPS 측정
  4. 텍스트 명령 "청기 올려" → 파싱 이벤트 + 상태 반영 확인
  5. 급격한 번복: "청기 내려" → 0.25s → "청기 올려" → 최종 상태/health 확인
  6. 알아들을 수 없는 명령 → deny 이벤트 확인
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"


def wait_http_ready(timeout: float = 30.0) -> dict:
    t0 = time.monotonic()
    last_err = None
    while time.monotonic() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"서버 준비 안 됨: {last_err}")


async def run_ws_tests() -> bool:
    results = {}

    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws", max_size=2**22) as ws:
        inbox: asyncio.Queue = asyncio.Queue()

        async def reader():
            async for msg in ws:
                inbox.put_nowait(msg)

        read_task = asyncio.create_task(reader())

        async def drain_for(seconds: float) -> tuple[int, int]:
            """seconds 동안 수신하며 (프레임 수, 상태 메시지 수) 집계."""
            frames = states = 0
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds:
                try:
                    msg = await asyncio.wait_for(inbox.get(), timeout=max(0.1, seconds))
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    frames += 1
                else:
                    data = json.loads(msg)
                    if data.get("type") == "state":
                        states += 1
            return frames, states

        async def send_text(text: str) -> None:
            await ws.send(json.dumps({"type": "text", "text": text}))

        async def wait_parsed(pred, timeout: float = 10.0) -> dict:
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                msg = await asyncio.wait_for(inbox.get(), timeout=timeout)
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get("type") == "parsed" and pred(data):
                        return data
            raise TimeoutError("parsed 이벤트 대기 시간 초과")

        async def wait_state(pred, timeout: float = 12.0) -> dict:
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                msg = await asyncio.wait_for(inbox.get(), timeout=timeout)
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get("type") == "state" and pred(data):
                        return data
            raise TimeoutError("state 조건 대기 시간 초과")

        print("=== 3. 프레임 스트림 3초 측정 ===")
        frames, states = await drain_for(3.0)
        fps = frames / 3.0
        results["stream_fps"] = fps
        print(f"   프레임 {frames}개 ({fps:.1f} fps), 상태 {states}회")
        ok_stream = fps >= 8  # llvmpipe 소프트웨어 GL 고려 낮은 기준
        print(f"   스트림: {'PASS' if ok_stream else 'FAIL'}")

        print("=== 4. 텍스트 명령: 청기 올려 (게스처: 올림→자동 복귀) ===")
        await send_text("청기 올려")
        ev = await wait_parsed(lambda d: d["applied"])
        print(f"   파싱: reply={ev['reply']!r} engine={ev['engine']}")
        st = await wait_state(lambda d: d["blue_up"] is True)
        print(f"   🟦 올림 반영!  tilt={st['tilt']:+.1f}° health={st['health']}")
        results["blue_up"] = st["blue_up"]
        print("   명령 반영: PASS" if st["blue_up"] else "   명령 반영: FAIL")
        t0 = time.monotonic()
        st2 = await wait_state(lambda d: d["blue_up"] is False, timeout=6.0)
        dt = time.monotonic() - t0
        print(f"   기본 동작 완료: {dt:.1f}s 만에 자동으로 내려옴 (phase={st2.get('blue_phase')})")
        results["gesture_return"] = st2["blue_up"] is False
        print("   올렸다→자동 내림: PASS" if results["gesture_return"] else "   자동 복귀: FAIL")

        print("=== 5. 급격한 번복: 내려 → 0.25s → 올려 ===")
        await send_text("청기 내려")
        await asyncio.sleep(0.25)
        await send_text("청기 올려")
        t0 = time.monotonic()
        st = await wait_state(lambda d: d["blue_up"] is True and abs(d["tilt"]) < 15 and d["health"] > 0.3)
        dt = time.monotonic() - t0
        print(f"   번복 반영 {dt:.2f}s 후: 🟦{'올림' if st['blue_up'] else '내림'} "
              f"tilt={st['tilt']:+.1f}° health={st['health']}")
        results["reversal_ok"] = st["blue_up"] and st["health"] > 0.3

        print("=== 5.5 음성 시뮬레이션: interim → 번복 → final ===")
        async def send_voice(text, final):
            await ws.send(json.dumps({"type": "voice", "text": text, "final": final}))

        # 직전 상태(청기 올림)와 달라야 interim 반영이 보인다: '내려'부터
        await send_voice("청기 내려", False)
        ev = await wait_parsed(lambda d: d["source"] == "voice_interim" and d["applied"])
        print(f"   interim 반영: {ev['text']!r} → {ev['reply']!r}")
        st = await wait_state(lambda d: d["blue_up"] is False)
        # 말하다가 번복: "…잠깐, 올려" → 즉시 갈아탐
        await send_voice("청기 올려", False)
        ev = await wait_parsed(lambda d: d["source"] == "voice_interim" and d["applied"])
        print(f"   interim 번복: {ev['text']!r} → {ev['reply']!r}")
        st = await wait_state(lambda d: d["blue_up"] is True)
        # 말 끝 (final): 같은 명령 확정 → 중복 실행 없음 (dedup)
        await send_voice("청기 올려", True)
        ev = await wait_parsed(lambda d: d["source"] == "voice_final")
        print(f"   final 확정: applied={ev['applied']} (중복 실행 방지)")
        results["voice_flow"] = (st is not None) and (ev["applied"] is False)
        print("   음성 interim/번복/dedup: PASS" if results["voice_flow"] else "   음성 흐름: FAIL")

        print("=== 6. 알 수 없는 명령 → deny ===")
        await send_text("피자 먹고 싶다")
        ev = await wait_parsed(lambda d: d["unknown"] and d["applied"])
        print(f"   deny 이벤트: reply={ev['reply']!r}")
        results["deny"] = True

        read_task.cancel()

    passed = (ok_stream and results.get("blue_up") and results.get("gesture_return")
              and results.get("reversal_ok") and results.get("deny") and results.get("voice_flow"))
    print("\n" + ("🎉 웹 통합 PASS" if passed else "❌ 웹 통합 FAIL"))
    return bool(passed)


def main() -> int:
    print("=== 1. 서버 실행 (백그라운드) ===")
    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "web_server.py"),
         "--port", str(PORT), "--fps", "20"],
        cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        print("=== 2. /health 대기 ===")
        health = wait_http_ready()
        print(f"   서버 OK: engine={health['engine']}")
        ok = asyncio.run(run_ws_tests())
        return 0 if ok else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        print("서버 종료 완료")


if __name__ == "__main__":
    sys.exit(main())
