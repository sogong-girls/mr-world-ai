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
- Swagger(직접 요청해 보기): http://localhost:8000/docs
- 투어 정보는 백엔드(`BACKEND_BASE_URL`)의 `GET /api/tours`, `GET /api/tours/{id}`에서 받는다. 백엔드가 꺼져 있으면 503.
- 터미널 대화: `.venv\Scripts\python -m scripts.chat_cli` (백엔드 없이: `--fake`)
- 테스트: `.venv\Scripts\python -m pytest -q` (OpenAI, 백엔드 없이 돈다)
- API 명세 다시 만들기: `.venv\Scripts\python -m scripts.build_openapi` → [docs/ai-api.json](docs/ai-api.json)
- Docker: `docker build -t mr-world-ai .` → `docker run --env-file .env -p 8000:8000 mr-world-ai`

## API (nginx: `/api/ai/` → AI 서버, `/api/` → 백엔드)

| API | 요청 | 응답 |
|---|---|---|
| `POST /api/ai/text-commands` | JSON `{text, context}` | CommandResponse |
| `POST /api/ai/voice-commands` | multipart: `audio`(webm, wav, m4a, mp3, 15초 이하), `context`(CommandContext JSON 문자열) | CommandResponse |
| `GET /api/ai/health` | | `{status, openaiKeySet, nluModel, sttModel, backend, tours}` |

**context (CommandContext)**: 서버는 대화를 기억하지 않는다. 프론트가 전역 저장소에 두었다가 매번 보낸다.
- `draft`: 지난 응답의 `draft` 그대로
- `confirmedFields`: 지난 응답의 `confirmedFields` 그대로
  (확인할 7개: `tourId, participantsCount, tourStyleId, hotelOptionId, foodOptionId, bookingAddons, startAt`)
- `pastBookings`: 백엔드 `GET /api/users/me/bookings`(BookingResponse) 목록, 최근순. 이름이 같아 그대로 넣으면 된다
- `lastTranscripts`: 고객이 최근에 한 말 3개

서버는 받은 draft를 지금 투어 정보로 다시 검사한다. 지난 출발일, 없어진 투어나 옵션은 비우고 경고(`warnings`)로 알려 준다.

**CommandResponse**: `transcript, intent, draft, changedFields, confirmedFields, missingFields, reply, warnings`

## 프론트 처리 규칙

| 상황 | 할 일 |
|---|---|
| 모든 응답 | `reply`를 말풍선으로, `draft`와 `confirmedFields`는 받은 것으로 **교체** |
| `intent = SUBMIT_BOOKING`이고 `missingFields = []` | draft로 옵션이 모두 선택된 예약 페이지로 이동 → 고객이 [예약하기] → 백엔드 `POST /api/bookings`에 draft를 그대로 보냄 (출발, 도착 시각은 투어 시각과 같아야 백엔드가 받으므로 고치지 않는다) |
| `intent = CHECK_PRICE` | `tourStyleId, hotelOptionId, foodOptionId, participantsCount`가 차 있으면 백엔드 `POST /api/tours/{tourId}/price`에 그 4개와 `addonOptions: draft.bookingAddons`를 보냄 → 정가, 할인가, 최종가를 채팅에 표시 |
| `missingFields`가 남음 | 앞 1~2개 항목의 선택지 카드를 보여 줄 수 있다 (카드를 누르면 카드 문구를 text-commands로) |
| `warnings` | 규칙을 어겨 반영하지 않은 항목. 문장은 이미 `reply` 앞에 들어 있다 |

draft는 백엔드 `BookingCreateRequest`와 이름, 모양이 같다: `bookingAddons: [{addonOptionId, nthDay, meal}]`,
`startAt`/`endAt`은 시간대 없는 한국 시간(`2026-12-03T09:00:00`). 비어 있는 칸은 `null`.

**요청할 때**
- 응답은 보통 글 1.3~1.5초, 음성 약 3초 (2026-11-09 측정). 그동안 "생각 중" 표시를 띄운다.
- **요청마다 20초 제한**(AbortController)을 걸고, 넘으면 "다시 말씀해 주세요"를 보여 준다. OpenAI 쪽 네트워크가 막히면 응답이 오래 걸릴 수 있다.
- 쿠키 로그인 때문에 요청을 credentials 포함으로 보내도 된다 (AI 서버가 `CORS_ORIGINS`의 주소에 허용).
- 오류 형식이 백엔드와 다르다: 백엔드는 글자(text), AI는 아래 JSON.

## 오류 `{status, code, message}`

| status | code | 언제 |
|---|---|---|
| 400 | `INVALID_REQUEST` | 요청 형식 오류 (빈 문장, context JSON 오류) |
| 400 | `UNSUPPORTED_AUDIO` | 읽을 수 없는 음성 형식 |
| 413 | `AUDIO_TOO_LARGE` | 5MB 또는 15초 초과 |
| 422 | `NO_SPEECH` | 말소리가 없음 |
| 429 | `RATE_LIMITED` | 한 IP가 1분에 30번 넘게 부름 |
| 502 / 504 | `UPSTREAM_ERROR` / `UPSTREAM_TIMEOUT` | OpenAI 음성 인식 실패, 지연 |
| 503 | `CATALOG_UNAVAILABLE` | 백엔드에서 투어 정보를 한 번도 받지 못함 |
| 503 | `STT_UNAVAILABLE` | OpenAI 키가 없어 음성 인식 불가 (글로 입력) |

의도 파악은 OpenAI가 실패하면 규칙으로 대신 처리하므로 오류가 나지 않는다.
