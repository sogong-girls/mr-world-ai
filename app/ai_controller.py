"""AIController: 프론트(WebApp)의 요청을 받아 ChatService에 맡긴다. 백엔드의 Controller와 같은 역할.

voiceCommand()  POST /api/ai/voice-commands   음성(multipart) → CommandResponse
textCommand()   POST /api/ai/text-commands    문장(JSON) → CommandResponse
health()        GET  /api/ai/health           OpenAI 키, 백엔드 연결 상태
_guard()        요청을 처리하기 전 검사: 한 IP가 1분에 너무 많이 부르면 막는다 (OpenAI 비용 보호)
"""
import time
from collections import defaultdict, deque

from fastapi import File, Form, Request, UploadFile
from pydantic import ValidationError

from app.chat_service import ChatService
from app.config import Settings
from app.dto import CommandContext, CommandResponse, TextCommandRequest
from app.errors import ApiError
from app.tour_catalog import CatalogUnavailableError, TourCatalog

REQUESTS_PER_MINUTE = 30  # IP 하나당 1분에 부를 수 있는 횟수


class AIController:
    def __init__(self, chatService: ChatService, catalog: TourCatalog, settings: Settings):
        self.chatService = chatService
        self.catalog = catalog
        self.settings = settings
        self._calls: dict[str, deque] = defaultdict(deque)  # IP → 최근 1분 동안 부른 시각들

    def voiceCommand(self, request: Request, audio: UploadFile = File(..., description="녹음한 음성 (webm, wav, m4a, mp3)"),
                     context: str | None = Form(None, description="CommandContext JSON 문자열")) -> CommandResponse:
        """음성으로 말하기: 음성 인식 → 뜻 파악 → draft 반영 → 다음 질문."""
        self._guard(request)
        ctx = None
        if context:
            try:
                ctx = CommandContext.model_validate_json(context)
            except ValidationError as error:
                raise ApiError(400, "INVALID_REQUEST", "context JSON 형식이 올바르지 않아요.") from error
        return self.chatService.replyToVoice(audio.file.read(), ctx, audio.content_type)

    def textCommand(self, request: Request, body: TextCommandRequest) -> CommandResponse:
        """타이핑하거나 선택지 카드를 누르기: 음성 인식 없이 같은 처리."""
        self._guard(request)
        return self.chatService.reply(body.text.strip(), body.context)

    def health(self) -> dict:
        """서버 상태. 백엔드가 꺼져 있어도 오류 대신 상태만 알려 준다."""
        try:
            tourCount = len(self.catalog.getAllTours())
        except CatalogUnavailableError:
            tourCount = 0
        return {
            "status": "ok" if tourCount else "degraded",
            "openaiKeySet": bool(self.settings.openai_api_key),  # False면 규칙으로만 해석, 음성 인식 불가
            "nluModel": self.settings.openai_model,
            "sttModel": self.settings.stt_model,
            "backend": self.catalog.backendStatus,
            "tours": tourCount,
        }

    def _guard(self, request: Request):
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        calls = self._calls[ip]
        while calls and now - calls[0] > 60:  # 1분이 지난 기록은 버린다
            calls.popleft()
        if len(calls) >= REQUESTS_PER_MINUTE:
            raise ApiError(429, "RATE_LIMITED", "요청이 너무 많아요. 잠시 후 다시 시도해 주세요.")
        calls.append(now)
