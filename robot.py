#!/usr/bin/env python3
"""청기백기 휴머노이드 로봇 컨트롤러 (MuJoCo 4).

핵심 설계:
  1. 위치 서보(position actuator) + 최소저크(min-jerk) 궤적 → 사람처럼 빠릿한 동작
  2. '팔꿈치 먼저 접고 → 어깨로 스윙' 궤적 (사람의 자연스러운 빠른 팔 올림)
     → 스윙 관성을 크게 줄여 균형을 유지함
  3. 저게인 자세 트림(pitch/roll) 루프 → 동작 후 몸을 느긋하게 원위치
  4. 깃발 천은 수동 힌지 → 빠른 동작에 살랑살랑 펄럭임

오른팔 = 청기(파랑), 왼팔 = 백기(흰색).
"""
from __future__ import annotations

import math
import os
from collections import deque

import mujoco
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XML = os.path.join(HERE, "humanoid_flags.xml")

# 서 있는 기본 자세 (모든 관절 ctrl 목표값)
STAND = {
    "r_shoulder_pitch": 0.30, "r_shoulder_roll": 0.12, "r_elbow": 0.55,
    "l_shoulder_pitch": 0.30, "l_shoulder_roll": 0.12, "l_elbow": 0.55,
    "r_hip_pitch": 0.08, "r_hip_roll": 0.0, "r_knee": 0.16, "r_ankle_pitch": -0.08, "r_ankle_roll": 0.0,
    "l_hip_pitch": 0.08, "l_hip_roll": 0.0, "l_knee": 0.16, "l_ankle_pitch": -0.08, "l_ankle_roll": 0.0,
}

# 팔 목표 자세
POSE_UP = {"shoulder_pitch": 3.00, "shoulder_roll": 0.35, "elbow": 0.40}
POSE_READY = {"shoulder_pitch": 0.30, "shoulder_roll": 0.12, "elbow": 0.55}
POSE_FOLD = {"shoulder_pitch": 0.30, "shoulder_roll": 0.35, "elbow": 1.90}  # 팔꿈치 접기(중간 자세)

# 동작 시간 (초) — 사람처럼 빠릿!
T_FOLD_UP = 0.16    # 올릴 때: 팔꿈치 접기
T_SWING_UP = 0.32   # 올릴 때: 어깨 스윙
T_SWING_DOWN = 0.32 # 내릴 때: 어깨 스윙
T_FOLD_DOWN = 0.16  # 내릴 때: 팔꿈치 펴기

# 자세 트림 게인 (너무 세면 발목이 '펄럭' 불안정 → 낮게 유지)
TRIM_KP = 0.50
TRIM_KD = 0.35
TRIM_CAP = 0.12
TRIM_TAU = 0.25  # 트림 명령 저역통과 시정수(초): 발 '찰십' 방지

# 깃발 ↔ 팔 매핑
FLAG_TO_SIDE = {"blue": "r", "white": "l", "both": ("r", "l"), "none": ()}

FLAG_KR = {"blue": "청기", "white": "백기", "both": "청기·백기", "none": ""}


def min_jerk(u: float) -> float:
    """최소저크 보간 (0→1 부드럽고 빠름)."""
    return 10 * u**3 - 15 * u**4 + 6 * u**5


class _Segment:
    """한 관절 그룹의 궤적 구간. 시작값은 실행 시점의 현재 ctrl에서 캡처."""

    __slots__ = ("targets", "duration", "elapsed")

    def __init__(self, targets: dict, duration: float):
        self.targets = targets          # {actuator 이름(접미사): 목표값}
        self.duration = duration
        self.elapsed = 0.0


class FlagRobot:
    def __init__(self, xml_path: str = DEFAULT_XML):
        self.model = mujoco.MjModel.from_xml_path(xml_path)
        self.data = mujoco.MjData(self.model)
        self.time = 0.0

        # 액추에이터/관절 인덱스 캐시
        self._act = {}
        self._jadr = {}
        for i in range(self.model.nu):
            name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, i)
            if name and name.startswith("act_"):
                self._act[name[4:]] = i
        for i in range(self.model.njnt):
            name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_JOINT, i)
            if name:
                self._jadr[name] = self.model.jnt_qposadr[i]

        self._pelvis = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "pelvis")
        self._hands = {
            "r": mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "r_hand"),
            "l": mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "l_hand"),
        }

        # 팔별 궤적 큐 (명령 중복 시 덮어씀 → 반응성 좋음)
        self._queues = {"r": deque(), "l": deque()}
        self._active = {"r": (None, None), "l": (None, None)}  # (segment, {이름: 시작값})

        # 자세 트림 상태
        self._trim_state = [0.0, 0.0]
        self._trim_cmd = [0.0, 0.0]  # 저역통과된 트림 명령

        # 초기화: 서있는 자세에서 시작
        self.data.qpos[2] = 0.93
        for name, val in STAND.items():
            self.data.qpos[self._jadr[name]] = val
            self._set_ctrl(name, val)
        mujoco.mj_forward(self.model, self.data)

    # ------------------------------------------------------------------ 기본
    def _set_ctrl(self, name: str, value: float) -> None:
        idx = self._act.get(name)
        if idx is not None:
            self.data.ctrl[idx] = value

    def _get_ctrl(self, name: str) -> float:
        return float(self.data.ctrl[self._act[name]])

    def _pelvis_tilt(self) -> tuple[float, float]:
        """(phi, psi): phi>0 = 앞으로 기울음, psi>0 = 왼쪽(+y)으로 기울음."""
        R = self.data.xmat[self._pelvis].reshape(3, 3)
        phi = math.asin(max(-1.0, min(1.0, R[2, 0])))
        psi = math.asin(max(-1.0, min(1.0, R[2, 1])))
        return phi, psi

    # ---------------------------------------------------------- 자세 트림
    def _posture_trim(self) -> None:
        phi, psi = self._pelvis_tilt()
        dt = self.model.opt.timestep
        dphi = (phi - self._trim_state[0]) / dt
        dpsi = (psi - self._trim_state[1]) / dt
        self._trim_state[0], self._trim_state[1] = phi, psi

        # PD 법칙 → 저역통과 필터 적용 (급격한 발목 슬랩 방지)
        cp_target = -max(-TRIM_CAP, min(TRIM_CAP, TRIM_KP * phi + TRIM_KD * dphi))
        cr_target = -max(-TRIM_CAP, min(TRIM_CAP, TRIM_KP * psi + TRIM_KD * dpsi))
        alpha = min(1.0, dt / TRIM_TAU)
        self._trim_cmd[0] += alpha * (cp_target - self._trim_cmd[0])
        self._trim_cmd[1] += alpha * (cr_target - self._trim_cmd[1])
        cp, cr = self._trim_cmd

        self._set_ctrl("r_ankle_pitch", STAND["r_ankle_pitch"] + cp)
        self._set_ctrl("l_ankle_pitch", STAND["l_ankle_pitch"] + cp)
        self._set_ctrl("r_ankle_roll", STAND["r_ankle_roll"] + cr)
        self._set_ctrl("l_ankle_roll", STAND["l_ankle_roll"] + cr)

    # ---------------------------------------------------------- 궤적 엔진
    def _push_segments(self, sides: tuple, segments: list[_Segment], interrupt: bool = True) -> None:
        for s in sides:
            if s not in ("r", "l"):
                continue
            q = self._queues[s]
            if interrupt:
                q.clear()
                self._active[s] = (None, None)
            q.extend(segments)

    def _advance_trajectories(self, dt: float) -> None:
        for s in ("r", "l"):
            seg, starts = self._active[s]
            if seg is None:
                if self._queues[s]:
                    seg = self._queues[s].popleft()
                    starts = {n: self._get_ctrl(f"{s}_{n}") for n in seg.targets}
                    self._active[s] = (seg, starts)
                else:
                    continue
            seg.elapsed += dt
            u = min(1.0, seg.elapsed / seg.duration)
            alpha = min_jerk(u)
            for name, target in seg.targets.items():
                value = starts[name] + alpha * (target - starts[name])
                self._set_ctrl(f"{s}_{name}", value)
            if u >= 1.0:
                for name, target in seg.targets.items():
                    self._set_ctrl(f"{s}_{name}", target)
                self._active[s] = (None, None)

    def busy(self) -> float:
        """남은 동작 시간 (초). 0이면 새 명령 즉시 가능."""
        total = 0.0
        for s in ("r", "l"):
            seg, _ = self._active[s]
            if seg is not None:
                total += seg.duration - seg.elapsed
            total += sum(seg.duration for seg in self._queues[s])
        return total

    # ---------------------------------------------------------- 게임 명령
    def command(self, flag: str, direction: str) -> str:
        """flag: 'blue'|'white'|'both'|'none', direction: 'up'|'down'|'hold'.

        반환: 수행 멘트 (한국어).
        """
        if flag not in FLAG_TO_SIDE:
            flag = "none"
        sides = FLAG_TO_SIDE[flag]
        if isinstance(sides, str):
            sides = (sides,)
        if not sides or direction == "hold":
            return f"{FLAG_KR[flag]} 안 움직여요!"

        if direction == "up":
            segs = [
                _Segment({"elbow": POSE_FOLD["elbow"], "shoulder_roll": POSE_FOLD["shoulder_roll"]}, T_FOLD_UP),
                _Segment({"shoulder_pitch": POSE_UP["shoulder_pitch"], "elbow": POSE_UP["elbow"]}, T_SWING_UP),
            ]
        elif direction == "down":
            segs = [
                _Segment({"shoulder_pitch": POSE_READY["shoulder_pitch"], "elbow": POSE_FOLD["elbow"]}, T_SWING_DOWN),
                _Segment({"elbow": POSE_READY["elbow"], "shoulder_roll": POSE_READY["shoulder_roll"]}, T_FOLD_DOWN),
            ]
        else:
            return f"알 수 없는 동작이에요: {direction}"

        self._push_segments(sides, segs)
        verb = "올려!" if direction == "up" else "내려!"
        return f"{FLAG_KR[flag]} {verb}"

    def deny(self) -> str:
        """알아들을 수 없는 명령 → 어깨 으쓱+팔꿈치 펄럭 거부 동작."""
        wiggle = []
        e0 = POSE_READY["elbow"]
        for target, dur in ((1.6, 0.10), (e0, 0.08), (1.6, 0.10), (e0, 0.08)):
            wiggle.append(_Segment({"elbow": target}, dur))
        self._push_segments(("r", "l"), wiggle)
        return "무슨 말인지 모르겠어요!"

    def salute(self) -> str:
        """게임 시작 인사: 양깃발 들었다 내리기."""
        up = self.command("both", "up")
        for s in ("r", "l"):
            self._queues[s].extend([
                _Segment({"shoulder_pitch": POSE_READY["shoulder_pitch"], "elbow": POSE_FOLD["elbow"]}, T_SWING_DOWN),
                _Segment({"elbow": POSE_READY["elbow"], "shoulder_roll": POSE_READY["shoulder_roll"]}, T_FOLD_DOWN),
            ])
        return "게임 시작! " + up

    # ---------------------------------------------------------- 상태/유틸
    def flag_heights(self) -> dict:
        """깃대 손의 높이 (m). 게임 상태 표시용."""
        out = {}
        for side, name in (("blue", "r"), ("white", "l")):
            out[side] = float(self.data.xpos[self._hands[name]][2])
        return out

    def flags_up(self) -> dict:
        return {f: h > 1.35 for f, h in self.flag_heights().items()}

    def standing_health(self) -> float:
        """0~1: 서있는 정도 (1=완벽 서 있음, 0=쓰러짐)."""
        phi, psi = self._pelvis_tilt()
        z = self.data.qpos[2]
        upright = max(0.0, 1.0 - (abs(phi) + abs(psi)) / 0.35)
        height = max(0.0, min(1.0, (z - 0.4) / 0.5))
        return round(0.6 * upright + 0.4 * height, 3)

    def step(self, dt: float | None = None) -> None:
        """물리 1스텝 (기본 1ms). 궤적 진행 + 자세 트림 + 시뮬레이션."""
        if dt is None:
            dt = self.model.opt.timestep
        self._advance_trajectories(dt)
        self._posture_trim()
        mujoco.mj_step(self.model, self.data)
        self.time += dt

    def run_for(self, seconds: float) -> None:
        n = int(seconds / self.model.opt.timestep)
        for _ in range(n):
            self.step()

    def settle(self, seconds: float = 1.2) -> None:
        """착지 후 안정화."""
        self.run_for(seconds)

    @property
    def pelvis_tilt_deg(self) -> tuple[float, float]:
        phi, psi = self._pelvis_tilt()
        return math.degrees(phi), math.degrees(psi)
