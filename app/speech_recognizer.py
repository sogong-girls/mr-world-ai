import io
import os
import shutil
import subprocess
import tempfile
import wave
import imageio_ffmpeg
import webrtcvad
from openai import APITimeoutError, OpenAI, OpenAIError

from app.config import Settings
from app.errors import ApiError
from app.tour_catalog import Tour

SAMPLE_RATE = 16000                               # VAD와 Whisper가 잘 다루는 16kHz
FRAME_MS = 30                                     # VAD는 10, 20, 30ms 조각만 판단한다
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 16비트 = 2바이트
PAD_FRAMES = 10                                   # 말소리 앞뒤 300ms는 남긴다 (첫, 끝 음절 잘림 방지)
VAD_LEVEL = 2                                     # 0(느슨)~3(엄격)
MAX_AUDIO_BYTES = 5 * 1024 * 1024                 # 5MB
MAX_AUDIO_SEC = 15                                # 한 번에 15초까지
MIN_SPEECH_MS = 300                               # 말소리가 이보다 짧으면 말하지 않은 것으로 본다

AUDIO_TYPES = {  # 받는 형식 → 임시 파일 확장자
    "audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "application/octet-stream": ".bin",  # 형식을 안 붙여 보내면 ffmpeg가 알아서 읽어 본다
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
        """음성 → 무음 제거 → Whisper → 문장."""
        wav = self._trimSilence(audio, contentType)
        if self._client is None:
            raise ApiError(503, "STT_UNAVAILABLE", "음성 인식을 쓸 수 없어요. 글로 입력해 주세요.")
        hint = ", ".join(tour.name for tour in tours) + ", " + BASE_WORDS
        try:
            result = self._client.audio.transcriptions.create(
                model=self.settings.stt_model,
                file=("speech.wav", wav, "audio/wav"),
                language="ko",
                prompt=hint,
                temperature=0,
            )
        except APITimeoutError as error:
            raise ApiError(504, "UPSTREAM_TIMEOUT", "음성 인식이 늦어지고 있어요. 다시 말씀해 주세요.") from error
        except OpenAIError as error:
            print(f"[SpeechRecognizer] 음성 인식 실패: {error}")
            raise ApiError(502, "UPSTREAM_ERROR", "음성 인식에 문제가 생겼어요. 글로 입력해 주세요.") from error
        return result.text.strip()

    def _trimSilence(self, audio: bytes, contentType: str | None) -> bytes:
        """받은 음성을 16kHz 모노로 바꾸고 말소리 부분만 남긴 WAV를 돌려준다."""
        if not audio:
            raise ApiError(400, "INVALID_REQUEST", "음성 파일이 비어 있어요.")
        if len(audio) > MAX_AUDIO_BYTES:
            raise ApiError(413, "AUDIO_TOO_LARGE", "음성은 5MB 이하로 보내 주세요.")
        kind = (contentType or "application/octet-stream").split(";")[0].strip().lower()
        if kind not in AUDIO_TYPES:
            raise ApiError(400, "UNSUPPORTED_AUDIO", f"지원하지 않는 음성 형식이에요 ({kind}).")

        pcm = decode_to_pcm(audio, AUDIO_TYPES[kind])
        if len(pcm) > MAX_AUDIO_SEC * SAMPLE_RATE * 2 + SAMPLE_RATE:  # 0.5초 여유
            raise ApiError(413, "AUDIO_TOO_LARGE", f"한 번에 {MAX_AUDIO_SEC}초 이내로 말씀해 주세요.")

        vad = webrtcvad.Vad(VAD_LEVEL)
        frames = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
        isSpeech = [vad.is_speech(frame, SAMPLE_RATE) for frame in frames]
        if sum(isSpeech) * FRAME_MS < MIN_SPEECH_MS:
            raise ApiError(422, "NO_SPEECH", "말소리가 들리지 않았어요. 다시 말씀해 주세요.")
        keep = [False] * len(frames)
        for i, speech in enumerate(isSpeech):
            if speech:  # 말소리 조각과 그 앞뒤 PAD_FRAMES개를 남긴다
                for j in range(max(0, i - PAD_FRAMES), min(len(frames), i + PAD_FRAMES + 1)):
                    keep[j] = True
        return pcm_to_wav(b"".join(frame for frame, k in zip(frames, keep) if k))


def ffmpeg_path() -> str:
    """컴퓨터에 설치된 ffmpeg, 없으면 imageio-ffmpeg 패키지에 들어 있는 것."""
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def decode_to_pcm(audio: bytes, suffix: str) -> bytes:
    """ffmpeg로 16kHz, 모노, 16비트 PCM으로 바꾼다 (m4a는 파이프로 못 읽는 경우가 있어 임시 파일을 쓴다)."""
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
