#!/usr/bin/env python3
"""청기백기 웹 서버 — MuJoCo 화면 + 텍스트/음성 명령을 브라우저로 제공.

실행:
  .venv/bin/python web_server.py                 # http://localhost:8000
  .venv/bin/python web_server.py --port 8000 --fps 25 --width 960 --height 540

구조:
  - 시뮬레이션 스레드: 1kHz 물리 스텝(실시간 페이싱) + 오프스크린 렌더 → JPEG
  - WebSocket (/ws):
      서버→클라: 바이너리 JPEG 프레임 (fps 간격) + JSON 상태/명령 이벤트
      클라→서버: {"type":"text"|"voice", "text":..., "final":bool}
  - 음성: 브라우저 Web Speech API(ko-KR)가 인식한 '중간 결과(interim)'를 즉시 전송
    → "청기 올려… 내려!" 처럼 말하는 도중 번복되는 명령도 로봇이 현재 동작을
    즉시 갈아타고 반영 (robot.py의 인터럽트 궤적 지원)

렌더링 백엔드: MUJOCO_GL (기본 egl — 이 서버에서 확인됨. 필요시 osmesa 등)
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import queue
import threading
import time
from collections import deque

# 반드시 mujoco import 전에 설정 (robot.py가 mujoco를 import 함)
os.environ.setdefault("MUJOCO_GL", "egl")

import mujoco  # noqa: E402
from PIL import Image  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from robot import FlagRobot, FLAG_TO_SIDE  # noqa: E402
from parser import CommandParser  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")


class GameServer:
    """로봇 + 물리 루프 + 렌더/인코딩 + 클라이언트 팬아웃 관리."""

    def __init__(self, args):
        self.args = args
        self.robot = FlagRobot()
        self.parser = CommandParser(engine=args.engine, model=args.model)

        self.width, self.height = args.width, args.height
        self.fps, self.quality = args.fps, args.quality
        # 주의: GL 렌더러는 '시뮬 스레드' 안에서 생성/사용한다 (GL 컨텍스트는 스레드 로컬)
        self.render_ok = True

        # 공유 상태 (lock 보호)
        self.lock = threading.Lock()
        self.latest_frame: bytes | None = None
        self.state: dict = {"blue_up": False, "white_up": False, "tilt": 0.0,
                            "health": 1.0, "fps": 0.0, "sim_lag": 0.0}
        self.clients: dict = {}          # id → asyncio.Queue
        self.loop: asyncio.AbstractEventLoop | None = None

        # 명령 (스레드 간)
        self.cmd_queue: queue.Queue = queue.Queue()
        self.running = True
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- 수명 주기
    def start(self) -> None:
        self._thread = threading.Thread(target=self._sim_loop, daemon=True, name="sim")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=3.0)

    # ------------------------------------------------------------- 명령 처리
    def _would_apply(self, actions: list) -> bool:
        """로봇 팔 단계(phase) 기준으로 이 명령이 실제로 움직일지 판정.

        게임 규칙: '올려'는 게스처(올림→유지→자동 복귀), '내려'는 번복, 'hold'는 정지.
        - 이미 올라가는 중/올려져 있으면 또 다른 '올려'는 무시
        - 이미 내려가는 중/내려가 있으면 또 다른 '내려'는 무시
        → 같은 명령 반복 스팸은 잡아주고, 진짜 번복(↑↔↓)은 즉시 통과!
        """
        phases = self.robot.arm_phases()
        for action in actions:
            d = action.get("dir")
            sides = FLAG_TO_SIDE.get(action.get("flag"), ())
            if isinstance(sides, str):
                sides = (sides,)
            if d in ("up", "down"):
                for s in sides:
                    ph = phases.get(s, "idle")
                    if d == "up" and ph not in ("up", "uphold"):
                        return True
                    if d == "down" and ph not in ("down", "idle"):
                        return True
            elif d == "hold":
                for s in (sides or ("r", "l")):
                    if phases.get(s) != "frozen":
                        return True
        return False

    def handle_command(self, text: str, source: str) -> dict:
        """WS 핸들러에서 스레드풀로 실행 (jev 네트워크 호출 포함).

        반환값은 모든 클라이언트에 브로드캐스트할 이벤트 dict.
        """
        parsed = self.parser.parse(text)
        unknown = bool(parsed.get("unknown")) and not parsed.get("actions")
        actions = parsed.get("actions") or []

        if unknown and source == "voice_interim":
            applied = False  # 말이 아직 덜 끝난 상태의 잡음 — 로봇을 흔들게 하지 않음
        elif unknown:
            self.cmd_queue.put(("deny", None))
            applied = True
        elif not actions:
            self.cmd_queue.put(("freeze", None))   # 가만히 / 손 대지 마 → 그 자리 정지
            applied = True
        elif self._would_apply(actions):
            self.cmd_queue.put(("cmd", parsed))
            applied = True
        else:
            applied = False  # 이미 하고 있는 동작 — 중복 무시

        return {
            "type": "parsed",
            "source": source,                      # text | voice_final | voice_interim
            "text": text,
            "reply": parsed.get("reply", ""),
            "engine": parsed.get("engine", ""),
            "unknown": unknown,
            "applied": applied,
        }

    # ------------------------------------------------------------- 브로드캐스트
    def fanout(self, payload, binary: bool) -> None:
        for q in list(self.clients.values()):
            try:
                q.put_nowait((binary, payload))
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # 느린 클라 → 오래된 프레임 드롭
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait((binary, payload))
                except asyncio.QueueFull:
                    pass

    def push_event(self, event: dict) -> None:
        msg = json.dumps(event, ensure_ascii=False)
        if self.loop:
            self.loop.call_soon_threadsafe(self.fanout, msg, False)

    async def stream_loop(self) -> None:
        """모든 클라에 프레임(fps 간격) + 상태(0.5s 간격) 전송."""
        interval = 1.0 / self.fps
        last_state = 0.0
        while self.running:
            t0 = time.perf_counter()
            with self.lock:
                frame = self.latest_frame
                state = dict(self.state)
            if frame:
                self.fanout(frame, binary=True)
            now = time.perf_counter()
            if now - last_state >= 0.5:
                self.fanout(json.dumps({"type": "state", **state}, ensure_ascii=False), False)
                last_state = now
            dt = time.perf_counter() - t0
            await asyncio.sleep(max(0.0, interval - dt))

    # ------------------------------------------------------------- 시뮬 루프
    def _sim_loop(self) -> None:
        robot = self.robot
        dt = robot.model.opt.timestep
        t0 = time.perf_counter()
        steps_done = 0
        next_frame = t0
        frame_times: deque = deque()

        # GL 렌더러는 이 스레드에서 생성 (컨텍스트 스레드 로컬)
        renderer = None
        camera = mujoco.MjvCamera()
        mujoco.mjv_defaultFreeCamera(robot.model, camera)
        # 상반신 + 든 깃발 클로즈업 (펄럭이는 천까지 여유 있게)
        camera.lookat[:] = [0.0, 0.0, 1.45]
        camera.distance = 3.2
        camera.azimuth = 18
        camera.elevation = 3
        try:
            renderer = mujoco.Renderer(robot.model, height=self.height, width=self.width)
        except Exception as e:
            self.render_ok = False
            print(f"⚠️ 오프스크린 렌더 불가 ({type(e).__name__}: {e}) — "
                  f"명령은 동작하지만 화면이 없습니다. MUJOCO_GL을 확인하세요 (egl/osmesa).")

        # 시작 인사
        robot.settle(1.0)
        robot.salute()
        next_frame = time.perf_counter()

        while self.running:
            now = time.perf_counter()

            # 1) 명령 소비 — 들어오는 즉시 반영 (번복 명령 → 현재 동작을 갈아탐)
            while True:
                try:
                    kind, payload = self.cmd_queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "cmd":
                    for action in payload["actions"]:
                        if action["dir"] in ("up", "down"):
                            robot.command(action["flag"], action["dir"])
                        elif action["dir"] == "hold":
                            robot.freeze(action["flag"])
                elif kind == "deny":
                    robot.deny()
                elif kind == "freeze":
                    robot.freeze("both")

            # 2) 물리 catch-up (실시간, 지연 누적 방지 캡 100ms)
            target = int((now - t0) / dt)
            want = min(target, steps_done + int(0.1 / dt))
            for _ in range(want - steps_done):
                robot.step()
            steps_done = want

            # 3) 프레임 렌더 + JPEG 인코딩 + 상태 스냅샷
            if renderer is not None and now >= next_frame:
                try:
                    renderer.update_scene(robot.data, camera=camera)
                    frame = renderer.render()
                    buf = io.BytesIO()
                    Image.fromarray(frame).save(buf, format="JPEG", quality=self.quality)
                    with self.lock:
                        self.latest_frame = buf.getvalue()
                        up = robot.flags_up()
                        phases = robot.arm_phases()
                        phi, _psi = robot.pelvis_tilt_deg
                        self.state = {
                            "blue_up": up["blue"],
                            "white_up": up["white"],
                            "blue_phase": phases["r"],
                            "white_phase": phases["l"],
                            "tilt": round(phi, 2),
                            "health": robot.standing_health(),
                            "fps": round(len(frame_times) / 3.0, 1),
                            "sim_lag": round((now - t0) - steps_done * dt, 3),
                        }
                except Exception as e:  # 렌더 일시 오류 → 다음 프레임 재시도
                    print(f"⚠️ 렌더 오류: {type(e).__name__}: {e}")
                frame_times.append(now)
                while frame_times and now - frame_times[0] > 3.0:
                    frame_times.popleft()
                next_frame = now + 1.0 / self.fps

            time.sleep(0.001)


# ----------------------------------------------------------------------------
# FastAPI 앱
# ----------------------------------------------------------------------------

game: GameServer | None = None
app = FastAPI(title="청기백기 휴머노이드", lifespan=None)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
def health() -> JSONResponse:
    with game.lock:
        state = dict(game.state)
    return JSONResponse({
        "status": "ok",
        "engine": game.parser.describe(),
        "render_ok": game.render_ok,
        "clients": len(game.clients),
        "state": state,
    })


async def _sender(sock: WebSocket, q: asyncio.Queue) -> None:
    while True:
        binary, payload = await q.get()
        if binary:
            await sock.send_bytes(payload)
        else:
            await sock.send_text(payload)


@app.websocket("/ws")
async def ws_endpoint(sock: WebSocket) -> None:
    await sock.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=60)
    cid = id(sock)
    game.clients[cid] = q
    sender_task = asyncio.create_task(_sender(sock, q))
    # 첫 인사: 파서 엔진 정보
    try:
        q.put_nowait((False, json.dumps(
            {"type": "hello", "engine": game.parser.active_engine}, ensure_ascii=False)))
    except asyncio.QueueFull:
        pass

    try:
        while True:
            raw = await sock.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            msg_type = data.get("type")
            text = str(data.get("text") or "").strip()[:120]
            if msg_type not in ("text", "voice") or not text:
                continue
            if msg_type == "text":
                source = "text"
            else:
                source = "voice_final" if data.get("final") else "voice_interim"
            # 파싱(jev 네트워크 호출)은 이벤트 루프를 막지 않도록 스레드풀로
            event = await asyncio.to_thread(game.handle_command, text, source)
            game.push_event(event)
    except WebSocketDisconnect:
        pass
    finally:
        game.clients.pop(cid, None)
        sender_task.cancel()


# ----------------------------------------------------------------------------
# 진입점
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="청기백기 웹 서버")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--height", type=int, default=540)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--quality", type=int, default=68)
    ap.add_argument("--engine", choices=["auto", "jev", "opencode", "offline"], default="auto")
    ap.add_argument("--model", default=None)
    ap.add_argument("--https", action="store_true",
                    help="자체 서명 인증서로 HTTPS 실행 — 모바일 브라우저 마이크 허용용 "
                         "(일반 http://IP 로 접속하면 모바일 마이크가 차단됨)")
    args = ap.parse_args()

    global game
    game = GameServer(args)
    game.start()

    scheme = "https" if args.https else "http"
    print()
    print("🟦⬜ 청기백기 웹 서버")
    print(f"   파서 엔진: {game.parser.describe()}")
    print(f"   로컬:    {scheme}://localhost:{args.port}")
    print(f"   원격:    {scheme}://<서버주소>:{args.port}  (또는 SSH 터널: ssh -L {args.port}:localhost:{args.port} 서버)")
    if args.https:
        print("   ⚠️ 자체 서명 인증서 — 브라우저 경고 화면에서 '고급 → 계속'을 눌러 진입하세요.")
        print("      모바일에서 마이크 권한이 허용되려면 HTTPS가 반드시 필요합니다.")
    else:
        print("   음성 인식은 '브라우저'에서 수행됩니다 (Chrome/Edge, Web Speech API ko-KR).")
        print("   모바일 직접 접속 + 음성을 원하면 --https 로 실행하세요.")
    print()

    import uvicorn
    ssl_kwargs = {}
    if args.https:
        cert_dir = os.path.join(HERE, ".certs")
        os.makedirs(cert_dir, exist_ok=True)
        cert_path = os.path.join(cert_dir, "cert.pem")
        key_path = os.path.join(cert_dir, "key.pem")
        if not (os.path.exists(cert_path) and os.path.exists(key_path)):
            import subprocess
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", key_path, "-out", cert_path, "-days", "3650",
                 "-subj", "/CN=flag-game"],
                check=True, capture_output=True,
            )
            print(f"   자체 서명 인증서 생성: {cert_path}")
        ssl_kwargs = {"ssl_certfile": cert_path, "ssl_keyfile": key_path}

    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level="warning", **ssl_kwargs)
    server = uvicorn.Server(config)

    async def serve():
        game.loop = asyncio.get_running_loop()
        streamer = asyncio.create_task(game.stream_loop())
        try:
            await server.serve()
        finally:
            streamer.cancel()
            game.stop()

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
