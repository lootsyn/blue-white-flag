#!/usr/bin/env python3
"""오프스크린 렌더 프리뷰 (test_headless.py / 사용자가 직접 실행).

로봇이 3가지 자세(기본/청기 업/양기 업)를 취한 장면을 preview/ 폴더에 PNG로 저장.
MUJOCO_GL 환경변수가 import 전에 설정되어야 하므로 필요시 별도 프로세스로 실행:
    MUJOCO_GL=egl python render_preview.py
"""
import os
import sys

import mujoco
import numpy as np

from robot import FlagRobot

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "preview")
os.makedirs(OUT, exist_ok=True)


def main() -> int:
    robot = FlagRobot()
    robot.settle(2.5)

    renderer = mujoco.Renderer(robot.model, height=900, width=1200)
    cam = mujoco.MjvCamera()
    mujoco.mjv_defaultFreeCamera(robot.model, cam)
    cam.lookat[:] = [0.0, 0.0, 1.45]   # 상반신 + 든 깃발 클로즈업
    cam.distance = 3.2
    cam.azimuth = 18
    cam.elevation = 3

    from PIL import Image

    def snap(name: str):
        robot.run_for(0.2)
        renderer.update_scene(robot.data, camera=cam)
        Image.fromarray(renderer.render()).save(os.path.join(OUT, f"{name}.png"))
        print(f"   saved preview/{name}.png")

    snap("01_ready")

    robot.command("blue", "up")
    while robot.busy() > 0:
        robot.step()
    snap("02_blue_up")

    robot.command("white", "up")
    while robot.busy() > 0:
        robot.step()
    snap("03_both_up")

    robot.command("both", "down")
    while robot.busy() > 0:
        robot.step()
    snap("04_ready_again")

    print("프리뷰 저장 완료!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
