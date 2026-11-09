import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_model: str
    stt_model: str
    backend_base_url: str
    catalog_ttl_sec: int
    cors_origins: list[str]


def load_settings() -> Settings:
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        stt_model=os.getenv("STT_MODEL", "whisper-1"),
        backend_base_url=os.getenv("BACKEND_BASE_URL", "http://localhost:8080").rstrip("/"),
        catalog_ttl_sec=int(os.getenv("CATALOG_TTL_SEC", "300")),
        # 브라우저에서 AI 서버를 직접 부르는 프론트 주소 (쉼표로 여러 개). nginx로 같은 주소면 필요 없음
        cors_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()],
    )
