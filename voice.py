#!/usr/bin/env python3
"""음성 입력 (STT) — sounddevice 녹음 + Google Web Speech (ko-KR).

의존성: sounddevice(마이크), SpeechRecognition(인식).
인터넷 연결 필요 (Google 무료 엔드포인트 사용).
마이크가 없으면 친절한 안내 후 텍스트 모드 안내.
"""
from __future__ import annotations

import io
import wave


class VoiceError(RuntimeError):
    pass


class VoiceInput:
    def __init__(self, samplerate: int = 16000, channels: int = 1):
        self.samplerate = samplerate
        self.channels = channels

    # ------------------------------------------------------------------ 검사
    def self_test(self) -> str | None:
        """마이크 사용 가능하면 None, 아니면 한국어 오류 메시지."""
        try:
            import sounddevice  # noqa: F401
        except Exception as e:
            return f"sounddevice 불가 ({e})"
        try:
            import speech_recognition  # noqa: F401
        except Exception as e:
            return f"SpeechRecognition 불가 ({e})"
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            has_input = any(d.get("max_input_channels", 0) > 0 for d in devices)
            if not has_input:
                return "입력 마이크가 없어요"
        except Exception as e:
            return f"오디오 장치 조회 실패 ({e})"
        return None

    # ------------------------------------------------------------------ 녹음
    def _record(self, seconds: float) -> bytes:
        """지정 초만큼 녹음해 16bit mono WAV 바이트로 반환."""
        import numpy as np
        import sounddevice as sd

        frames = int(seconds * self.samplerate)
        audio = sd.rec(frames, samplerate=self.samplerate,
                       channels=self.channels, dtype="int16")
        sd.wait()
        return audio.astype(np.int16).tobytes()

    def listen(self, phrase_limit: float = 4.0) -> str:
        """최대 phrase_limit 초 동안 듣고 한국어 문장 반환."""
        problem = self.self_test()
        if problem:
            raise VoiceError(f"마이크 사용 불가: {problem} → 텍스트 모드(--text)를 써 주세요")

        import speech_recognition as sr

        print(f"🎤 듣고 있어요… (최대 {phrase_limit:.0f}초, 말하세요!)")
        try:
            pcm = self._record(phrase_limit)
        except Exception as e:
            raise VoiceError(f"녹음 실패: {e}")

        audio = sr.AudioData(pcm, self.samplerate, 2)
        recognizer = sr.Recognizer()
        try:
            text = recognizer.recognize_google(audio, language="ko-KR")
        except sr.UnknownValueError:
            raise VoiceError("음성을 인식하지 못했어요 (좀 더 또박또박!)")
        except sr.RequestError as e:
            raise VoiceError(f"음성 인식 서버 오류 (인터넷 확인): {e}")

        text = (text or "").strip()
        if not text:
            raise VoiceError("빈 음성이에요")
        return text


if __name__ == "__main__":
    v = VoiceInput()
    print("셀프테스트:", v.self_test() or "마이크 OK")
    try:
        print("듣기 결과:", v.listen())
    except VoiceError as e:
        print("오류:", e)
