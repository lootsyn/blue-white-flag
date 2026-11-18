#!/usr/bin/env python3
"""청기백기 휴머노이드 헤드리스 테스트 (robot.py의 실제 컨트롤러 검증).

검증 항목:
  1. 서있기 안정화 (골반 높이/기울기)
  2. 청기 올리기 스냅 타임 (< 0.6s, 사람처럼 빠릿)
  3. 전체 명령 시퀀스 중 균형 (기울기 과도 < 14°, 추락 없음)
  4. 최종 서있는 상태 복귀
  5. (가능하면) 오프스크린 렌더 preview PNG 저장
"""
import math
import os
import sys

import numpy as np

from robot import FlagRobot

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    print("=== 1. 로봇 생성 & 안정화 ===")
    robot = FlagRobot()
    robot.settle(4.0)
    phi, psi = robot.pelvis_tilt_deg
    z = robot.data.qpos[2]
    print(f"   골반 z={z:.3f}m  pitch={phi:+.2f}°  roll={psi:+.2f}°  health={robot.standing_health()}")
    ok_stand = z > 0.85 and abs(phi) < 3.0 and abs(psi) < 3.0
    print(f"   서있기: {'PASS' if ok_stand else 'FAIL'}")

    print("=== 2. 청기(오른팔) 올리기 — 스냐� 측정 ===")
    jadr = robot._jadr["r_shoulder_pitch"]
    robot.command("blue", "up")
    t_snap = None
    t0 = robot.time
    while robot.time - t0 < (robot.busy() + 0.6):
        robot.step()
        if t_snap is None and robot.data.qpos[jadr] > 2.80:
            t_snap = robot.time - t0
    robot.run_for(0.6)
    phi, psi = robot.pelvis_tilt_deg
    print(f"   스냅 타임(2.8 rad 도달)={t_snap if t_snap else '>?'}s  이후 pitch={phi:+.2f}°")
    ok_snap = t_snap is not None and t_snap < 0.65
    print(f"   스냅: {'PASS' if ok_snap else 'FAIL'}")

    print("=== 3. 전체 명령 시퀀스 ===")
    seq = [
        ("white", "up"), ("blue", "down"), ("both", "up"),
        ("blue", "down"), ("white", "down"), ("both", "up"), ("both", "down"),
    ]
    peak_tilt = 0.0
    min_health = 1.0
    for flag, direction in seq:
        robot.command(flag, direction)
        while robot.busy() > 0:
            robot.step()
            phi, psi = robot.pelvis_tilt_deg
            peak_tilt = max(peak_tilt, abs(phi), abs(psi))
            min_health = min(min_health, robot.standing_health())
        robot.run_for(0.35)
        phi, psi = robot.pelvis_tilt_deg
        print(f"   {flag:>5} {direction:>4}: pitch={phi:+5.2f}°  z={robot.data.qpos[2]:.3f}")

    print("=== 4. 최종 상태 ===")
    robot.run_for(2.0)
    phi, psi = robot.pelvis_tilt_deg
    z = robot.data.qpos[2]
    health = robot.standing_health()
    print(f"   골반 z={z:.3f}m  pitch={phi:+.2f}°  roll={psi:+.2f}°  health={health}")
    print(f"   시퀀스 중 최대 기울기={peak_tilt:.1f}°  최저 health={min_health}")
    ok_balance = z > 0.85 and abs(phi) < 8.0 and peak_tilt < 14.0 and min_health > 0.3
    print(f"   균형 유지: {'PASS' if ok_balance else 'FAIL'}")

    ok_nan = bool(np.all(np.isfinite(robot.data.qpos)) and np.all(np.isfinite(robot.data.qvel)))
    print(f"=== 5. 수치 안정성: {'PASS' if ok_nan else 'FAIL'} ===")

    print("=== 6. 오프스크린 렌더 시도 ===")
    try_render()

    passed = ok_stand and ok_snap and ok_balance and ok_nan
    print("\n" + ("🎉 전체 PASS" if passed else "❌ FAIL — 튜닝 필요"))
    return 0 if passed else 1


def try_render():
    import subprocess
    for gl in ("egl", "glfw", "osmesa"):
        env = dict(os.environ, MUJOCO_GL=gl)
        try:
            r = subprocess.run(
                [sys.executable, os.path.join(HERE, "render_preview.py")],
                env=env, capture_output=True, text=True, timeout=180,
            )
            if r.returncode == 0:
                print(f"   MUJOCO_GL={gl}: 성공 → preview/*.png 저장 완료")
                return
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            print(f"   MUJOCO_GL={gl}: 불가 ({detail[-1][:110] if detail else '?'})")
        except Exception as e:
            print(f"   MUJOCO_GL={gl}: 실행 오류 ({e})")
    print("   → GL 백엔드 없음, 물리 테스트만으로 검증 (뷰어는 로컬 GUI 환경에서 실행)")


if __name__ == "__main__":
    sys.exit(main())
