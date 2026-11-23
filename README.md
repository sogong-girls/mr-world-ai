# Mr.World AI 서버

AI 채팅 예약(UC-06 Voice Order). 고객이 말하거나 타이핑하면 예약 폼(draft)에 고객이 말한 항목만 반영하고,
7개 항목(투어, 인원, 스타일, 호텔, 식사, 커피/샴페인, 출발일)을 모두 확인받을 때까지 다음 질문을 만든다.
설계 산출물의 AI 클래스도(AIController, ChatService, SpeechRecognizer, IntentParser, TourCatalog + DTO 4개)와 같은 구조임

## 환경 변수 (`.env`)

프로젝트 폴더에 `.env` 파일을 만들고 아래 값을 적는다. (`.env`는 비밀키가 있어 저장소에 올리지 않음)

| 이름 | 설명 | 기본값 |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API 키 (필수) | 없음 |
| `OPENAI_MODEL` | 의도 파악 모델 | `gpt-4.1-mini` |
| `STT_MODEL` | 음성 인식 모델 | `whisper-1` |
| `BACKEND_BASE_URL` | 백엔드 주소 | `http://localhost:8080` |
| `CATALOG_TTL_SEC` | 투어 정보 캐시 시간(초) | `300` |
| `CORS_ORIGINS` | 허용할 프론트 주소 (쉼표로 구분) | `http://localhost:5173` |

## 실행

```
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```
- API 문서(Swagger): 실행 후 `http://localhost:8000/docs`
- 투어 정보는 백엔드(`BACKEND_BASE_URL`)의 `GET /api/tours`, `GET /api/tours/{id}`에서 받는다.
