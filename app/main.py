from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.ai_controller import AIController
from app.chat_service import ChatService
from app.config import load_settings
from app.dto import CommandResponse
from app.errors import ApiError
from app.intent_parser import IntentParser
from app.speech_recognizer import SpeechRecognizer
from app.tour_catalog import CatalogUnavailableError, TourCatalog

VERSION = "1.0.0"
settings = load_settings()
catalog = TourCatalog(settings)
chatService = ChatService(catalog, IntentParser(settings), SpeechRecognizer(settings))
controller = AIController(chatService, catalog, settings)

app = FastAPI(title="Mr.World AI", version=VERSION,
              description="AI 채팅 예약: 음성이나 문장 → 예약 폼(draft) 채우기 + 다음 질문")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])



def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"status": status, "code": code, "message": message})


@app.exception_handler(ApiError)
def handle_api_error(request: Request, error: ApiError):
    return error_response(error.status, error.code, error.message)


@app.exception_handler(CatalogUnavailableError)
def handle_catalog_error(request: Request, error: CatalogUnavailableError):
    return error_response(503, "CATALOG_UNAVAILABLE", str(error))


@app.exception_handler(RequestValidationError)
def handle_validation_error(request: Request, error: RequestValidationError):
    # 백엔드처럼 요청 형식 오류는 400 (FastAPI 기본은 422)
    first = error.errors()[0] if error.errors() else {}
    where = ".".join(str(part) for part in first.get("loc", []) if part != "body")
    return error_response(400, "INVALID_REQUEST", f"요청 형식이 올바르지 않아요: {where} {first.get('msg', '')}".strip())



ERRORS = {
    400: {"description": "INVALID_REQUEST, UNSUPPORTED_AUDIO"},
    413: {"description": "AUDIO_TOO_LARGE"},
    422: {"description": "NO_SPEECH"},
    429: {"description": "RATE_LIMITED"},
    502: {"description": "UPSTREAM_ERROR (OpenAI 음성 인식 실패)"},
    503: {"description": "CATALOG_UNAVAILABLE (백엔드에서 투어 정보를 못 받음), STT_UNAVAILABLE"},
    504: {"description": "UPSTREAM_TIMEOUT"},
}
app.add_api_route("/api/ai/voice-commands", controller.voiceCommand, methods=["POST"],
                  response_model=CommandResponse, tags=["AI"], summary="음성 명령", responses=ERRORS)
app.add_api_route("/api/ai/text-commands", controller.textCommand, methods=["POST"],
                  response_model=CommandResponse, tags=["AI"], summary="텍스트 명령 (타이핑, 선택지 카드)",
                  responses=ERRORS)
app.add_api_route("/api/ai/health", controller.health, methods=["GET"], tags=["AI"], summary="상태 확인")
