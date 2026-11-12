import io
import os
import shutil
import subprocess
import tempfile
import wave
from collections import Counter

import imageio_ffmpeg
import webrtcvad
from openai import APITimeoutError, OpenAI, OpenAIError

from app.config import Settings
from app.errors import ApiError
from app.tour_catalog import Tour

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2
PAD_FRAMES = 10
VAD_LEVEL = 2
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_AUDIO_SEC = 15
MIN_SPEECH_MS = 300
NO_SPEECH_PROB = 0.5
MAX_COMPRESSION_RATIO = 2.4
MAX_REPEATS = 5

AUDIO_TYPES = {
    "audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "application/octet-stream": ".bin",
}
BASE_WORDS = ("클래식, 그랜드, 프리미엄, 3성급, 4성급, 5성급, 도시락, 현지식, 스테이크, 커피, 샴페인, "
              "첫째 날, 둘째 날, 마지막 날, 아침, 점심, 저녁, 출발, 예약, 허니문, 효도, 골프, 트레킹")


class SpeechRecognizer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        if settings.openai_api_key:
            self._client = OpenAI(api_key=settings.openai_api_key, timeout=20.0, max_retries=1)

    def transcribe(self, audio: bytes, tours: list[Tour], contentType: str | None = None) -> str:
        wav = self._trimSilence(audio, contentType)
        if self._client is None:
            raise ApiError(503, "STT_UNAVAILABLE", "음성 인식을 쓸 수 없어요. 글로 입력해 주세요.")
        hint = ", ".join(tour.name for tour in tours) + ", " + BASE_WORDS
        verbose = self.settings.stt_model.startswith("whisper")
        try:
            result = self._client.audio.transcriptions.create(
                model=self.settings.stt_model,
                file=("speech.wav", wav, "audio/wav"),
                language="ko",
                prompt=hint,
                temperature=0,
                response_format="verbose_json" if verbose else "json",
            )
        except APITimeoutError as error:
            raise ApiError(504, "UPSTREAM_TIMEOUT", "음성 인식이 늦어지고 있어요. 다시 말씀해 주세요.") from error
        except OpenAIError as error:
            print(f"[SpeechRecognizer] 음성 인식 실패: {error}")
            raise ApiError(502, "UPSTREAM_ERROR", "음성 인식에 문제가 생겼어요. 글로 입력해 주세요.") from error
        text = drop_hallucination(result, verbose)
        if text != result.text.strip():
            print(f"[SpeechRecognizer] 말이 아닌 소리로 보고 버림: {result.text[:80]!r}")
        return text

    def _trimSilence(self, audio: bytes, contentType: str | None) -> bytes:
        if not audio:
            raise ApiError(400, "INVALID_REQUEST", "음성 파일이 비어 있어요.")
        if len(audio) > MAX_AUDIO_BYTES:
            raise ApiError(413, "AUDIO_TOO_LARGE", "음성은 5MB 이하로 보내 주세요.")
        kind = (contentType or "application/octet-stream").split(";")[0].strip().lower()
        if kind not in AUDIO_TYPES:
            raise ApiError(400, "UNSUPPORTED_AUDIO", f"지원하지 않는 음성 형식이에요 ({kind}).")

        pcm = decode_to_pcm(audio, AUDIO_TYPES[kind])
        if len(pcm) > MAX_AUDIO_SEC * SAMPLE_RATE * 2 + SAMPLE_RATE:
            raise ApiError(413, "AUDIO_TOO_LARGE", f"한 번에 {MAX_AUDIO_SEC}초 이내로 말씀해 주세요.")

        vad = webrtcvad.Vad(VAD_LEVEL)
        frames = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
        isSpeech = [vad.is_speech(frame, SAMPLE_RATE) for frame in frames]
        if sum(isSpeech) * FRAME_MS < MIN_SPEECH_MS:
            raise ApiError(422, "NO_SPEECH", "말소리가 들리지 않았어요. 다시 말씀해 주세요.")
        keep = [False] * len(frames)
        for i, speech in enumerate(isSpeech):
            if speech:
                for j in range(max(0, i - PAD_FRAMES), min(len(frames), i + PAD_FRAMES + 1)):
                    keep[j] = True
        return pcm_to_wav(b"".join(frame for frame, k in zip(frames, keep) if k))


def drop_hallucination(result, verbose: bool) -> str:
    if verbose and result.segments is not None:
        kept = [s.text.strip() for s in result.segments
                if s.no_speech_prob < NO_SPEECH_PROB and s.compression_ratio < MAX_COMPRESSION_RATIO]
        text = " ".join(kept).strip()
    else:
        text = result.text.strip()
    words = text.replace(",", " ").split()
    if words and max(Counter(words).values()) >= MAX_REPEATS:
        return ""
    return text


def ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def decode_to_pcm(audio: bytes, suffix: str) -> bytes:
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(audio)
        result = subprocess.run(
            [ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
             "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, timeout=15,
        )
    finally:
        os.remove(path)
    if result.returncode != 0 or not result.stdout:
        raise ApiError(400, "UNSUPPORTED_AUDIO", "음성을 읽을 수 없어요. 다시 녹음해 주세요.")
    return result.stdout


def pcm_to_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(SAMPLE_RATE)
        file.writeframes(pcm)
    return buffer.getvalue()
