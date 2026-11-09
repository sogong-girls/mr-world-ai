# Mr.World AI 서버

AI 채팅 예약(UC-06 Voice Order). 고객이 말하거나 타이핑하면 예약 폼(draft)에 고객이 말한 항목만 반영하고,
7개 항목(투어, 인원, 스타일, 호텔, 식사, 커피/샴페인, 출발일)을 모두 확인받을 때까지 다음 질문을 만든다.
설계 산출물의 AI 클래스도(AIController, ChatService, SpeechRecognizer, IntentParser, TourCatalog + DTO 4개)와 같은 구조임

## 실행

```
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      (OPENAI_API_KEY 채우기)
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```
- 투어 정보는 백엔드(`BACKEND_BASE_URL`)의 `GET /api/tours`, `GET /api/tours/{id}`에서 받는다.
- 터미널 대화: `.venv\Scripts\python -m scripts.chat_cli` (백엔드 없이: `--fake`)
- 테스트: `.venv\Scripts\python -m pytest -q` (OpenAI, 백엔드 없이 돈다)
- API 명세 다시 만들기: `.venv\Scripts\python -m scripts.build_openapi` → [docs/ai-api.json](docs/ai-api.json)
