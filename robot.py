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
T_HOLD_TOP = 0.80   # 정점 유지 — 기본 동작: 올렸다가 잠깐 멈췄다 자동으로 내려온다

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

    __slots__ = ("targets", "duration", "elapsed", "phase")

    def __init__(self, targets: dict, duration: float, phase: str | None = None):
        self.targets = targets          # {actuator 이름(접미사): 목표값}
        self.duration = duration
        self.elapsed = 0.0
        self.phase = phase              # 구간 시작 시 설정할 팔 동작 단계


# 팔 동작 단계(phase): 게임 규칙과 중복 명령 판정의 기준
#   idle    : 기본(내려간) 대기
#   up      : 올라가는 중
#   uphold  : 정점 유지 중 (자동 복귀 대기)
#   down    : 내려가는 중 (번복/자동 복귀 포함)
#   frozen  : '올리지 마'로 그 자리에 얼어붙음


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
        self._phase = {"r": "idle", "l": "idle"}                # 팔 동작 단계

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
                    if seg.phase is not None:
                        self._phase[s] = seg.phase
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
                if not self._queues[s]:
                    self._phase[s] = "idle"

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

        게임 규칙 — 기본 동작은 '올렸다가 다시 내리는 것'까지:
          - 'up'   : 올리고 → 짧게 유지(T_HOLD_TOP) → 자동으로 내려온다 (게스처)
          - 'down' : 지금 위치에서 즉시 내린다 (번복 명령). 이미 내려가 있으면 무시
          - 'hold' : 그 자리에서 얼어붙기 ('올리지 마')
        이미 같은 동작을 하고 있는 팔은 건너뛴다 (중복 명령/스팸 방지).

        반환: 수행 멘트 (한국어).
        """
        if flag not in FLAG_TO_SIDE:
            flag = "none"
        if direction == "hold":
            return self.freeze(flag)

        sides = FLAG_TO_SIDE[flag]
        if isinstance(sides, str):
            sides = (sides,)
        if not sides or direction not in ("up", "down"):
            return f"{FLAG_KR[flag]} 안 움직여요!"

        acted = []
        for s in sides:
            phase = self._phase[s]
            if direction == "up":
                if phase in ("up", "uphold"):
                    continue  # 이미 올라가는 중 / 올려져 있음
                segs = [
                    _Segment({"elbow": POSE_FOLD["elbow"],
                              "shoulder_roll": POSE_FOLD["shoulder_roll"]}, T_FOLD_UP, phase="up"),
                    _Segment({"shoulder_pitch": POSE_UP["shoulder_pitch"],
                              "elbow": POSE_UP["elbow"]}, T_SWING_UP, phase="up"),
                    _Segment({}, T_HOLD_TOP, phase="uphold"),  # 정점 유지 (ctrl 그대로)
                    _Segment({"shoulder_pitch": POSE_READY["shoulder_pitch"],
                              "elbow": POSE_FOLD["elbow"]}, T_SWING_DOWN, phase="down"),
                    _Segment({"elbow": POSE_READY["elbow"],
                              "shoulder_roll": POSE_READY["shoulder_roll"]}, T_FOLD_DOWN, phase="down"),
                ]
            else:  # down: 번복 — 지금 위치에서 바로 내린다
                if phase in ("down", "idle"):
                    continue  # 이미 내려가는 중 / 내려가 있음
                segs = [
                    _Segment({"shoulder_pitch": POSE_READY["shoulder_pitch"],
                              "elbow": POSE_FOLD["elbow"]}, T_SWING_DOWN, phase="down"),
                    _Segment({"elbow": POSE_READY["elbow"],
                              "shoulder_roll": POSE_READY["shoulder_roll"]}, T_FOLD_DOWN, phase="down"),
                ]
            self._push_segments((s,), segs)
            self._phase[s] = direction if direction == "down" else "up"
            acted.append(s)

        if not acted:
            state = "올라 있어요!" if direction == "up" else "내려가 있어요!"
            return f"{FLAG_KR[flag]} 이미 {state}"
        verb = "올려!" if direction == "up" else "내려!"
        return f"{FLAG_KR[flag]} {verb}"

    def freeze(self, flag: str = "both") -> str:
        """'올리지 마' / '가만히' — 현재 자리에서 정지.

        남은 궤적을 제거하고 ctrl은 마지막 값을 유지 → 서보가 그 자세를 지탱한다.
        동작 중이라면 그 순간에 멈춘다 (진행자에게 딱 걸린 모습!).
        """
        sides = FLAG_TO_SIDE.get(flag, ())
        if isinstance(sides, str):
            sides = (sides,)
        if not sides:
            sides = ("r", "l")
        for s in sides:
            self._queues[s].clear()
            self._active[s] = (None, None)
            # 서보는 ctrl이 실제 각도보다 앞서 있음(궤적 리드). 리드의 30%만 남기면
            # 거의 그 자리에서 즉시 멈추고, 남은 오차만큼의 서보 토크로 자세를 지탱한다.
            for name in ("shoulder_pitch", "shoulder_roll", "elbow"):
                idx = self._act.get(f"{s}_{name}")
                jadr = self._jadr.get(f"{s}_{name}")
                if idx is None or jadr is None:
                    continue
                lead = float(self.data.ctrl[idx]) - float(self.data.qpos[jadr])
                self.data.ctrl[idx] = float(self.data.qpos[jadr]) + 0.3 * lead
            self._phase[s] = "frozen"
        return f"{FLAG_KR.get(flag, '')} 안 움직여요!".strip()

    def arm_phases(self) -> dict:
        """팔별 동작 단계 스냅샷 (웹 서버의 중복 명령 판정용)."""
        return dict(self._phase)

    def deny(self) -> str:
        """알아들을 수 없는 명령 → 어깨 으쓱+팔꿈치 펄럭 거부 동작."""
        wiggle = []
        e0 = POSE_READY["elbow"]
        for target, dur in ((1.6, 0.10), (e0, 0.08), (1.6, 0.10), (e0, 0.08)):
            wiggle.append(_Segment({"elbow": target}, dur))
        self._push_segments(("r", "l"), wiggle)
        return "무슨 말인지 모르겠어요!"

    def salute(self) -> str:
        """게임 시작 인사: 양깃발 올렸다가 자동으로 내리기 (기본 게스처)."""
        return "게임 시작! " + self.command("both", "up")

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
