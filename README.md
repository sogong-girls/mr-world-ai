# Mr.World AI 서버

AI 채팅 예약(UC-06 Voice Order). 고객이 말하거나 타이핑하면 **예약 폼(draft)에 고객이 말한 항목만 반영**하고,
7개 항목(투어, 인원, 스타일, 호텔, 식사, 커피/샴페인, 출발일)을 모두 확인받을 때까지 다음 질문을 만든다.
설계 산출물의 AI 클래스도(AIController, ChatService, SpeechRecognizer, IntentParser, TourCatalog + DTO 4개)와 같은 구조다.
클래스도와 코드의 대응: [docs/worklog.md](docs/worklog.md)

## 실행

```
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      (OPENAI_API_KEY 채우기)
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```
- 투어 정보는 백엔드(`BACKEND_BASE_URL`)의 `GET /api/tours`, `GET /api/tours/{id}`에서 받는다. 백엔드가 꺼져 있으면 503.
- 터미널 대화: `.venv\Scripts\python -m scripts.chat_cli` (백엔드 없이: `--fake`)
- 테스트: `.venv\Scripts\python -m pytest -q` (OpenAI, 백엔드 없이 돈다)
- API 명세 다시 만들기: `.venv\Scripts\python -m scripts.build_openapi` → [docs/ai-api.json](docs/ai-api.json)

| 429 | `RATE_LIMITED` | 한 IP가 1분에 30번 넘게 부름 |
| 502 / 504 | `UPSTREAM_ERROR` / `UPSTREAM_TIMEOUT` | OpenAI 음성 인식 실패, 지연 |
| 503 | `CATALOG_UNAVAILABLE` | 백엔드에서 투어 정보를 한 번도 받지 못함 |
| 503 | `STT_UNAVAILABLE` | OpenAI 키가 없어 음성 인식 불가 (글로 입력) |

의도 파악은 OpenAI가 실패하면 규칙으로 대신 처리하므로 오류가 나지 않는다.
