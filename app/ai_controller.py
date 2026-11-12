import time
from collections import defaultdict, deque

from fastapi import File, Form, Request, UploadFile
from pydantic import ValidationError

from app.chat_service import ChatService
from app.config import Settings
from app.dto import CommandContext, CommandResponse, TextCommandRequest
from app.errors import ApiError
from app.tour_catalog import CatalogUnavailableError, TourCatalog

REQUESTS_PER_MINUTE = 30


class AIController:
    def __init__(self, chatService: ChatService, catalog: TourCatalog, settings: Settings):
        self.chatService = chatService
        self.catalog = catalog
        self.settings = settings
        self._calls: dict[str, deque] = defaultdict(deque)

    def voiceCommand(self, request: Request, audio: UploadFile = File(..., description="녹음한 음성 (webm, wav, m4a, mp3)"),
                     context: str | None = Form(None, description="CommandContext JSON 문자열")) -> CommandResponse:
        self._guard(request)
        ctx = None
        if context:
            try:
                ctx = CommandContext.model_validate_json(context)
            except ValidationError as error:
                raise ApiError(400, "INVALID_REQUEST", "context JSON 형식이 올바르지 않아요.") from error
        return self.chatService.replyToVoice(audio.file.read(), ctx, audio.content_type)

    def textCommand(self, request: Request, body: TextCommandRequest) -> CommandResponse:
        self._guard(request)
        text = body.text.strip()
        if not text:
            raise ApiError(400, "INVALID_REQUEST", "내용을 입력해 주세요.")
        return self.chatService.reply(text, body.context)

    def health(self) -> dict:
        try:
            tourCount = len(self.catalog.getAllTours())
        except CatalogUnavailableError:
            tourCount = 0
        return {
            "status": "ok" if tourCount else "degraded",
            "openaiKeySet": bool(self.settings.openai_api_key),
            "nluModel": self.settings.openai_model,
            "sttModel": self.settings.stt_model,
            "backend": self.catalog.backendStatus,
            "tours": tourCount,
        }

    def _guard(self, request: Request):
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        calls = self._calls[ip]
        while calls and now - calls[0] > 60:
            calls.popleft()
        if len(calls) >= REQUESTS_PER_MINUTE:
            raise ApiError(429, "RATE_LIMITED", "요청이 너무 많아요. 잠시 후 다시 시도해 주세요.")
        calls.append(now)
