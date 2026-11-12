import re
from datetime import date, datetime, timedelta
from app.config import Settings
from app.dto import (KST, AddonSlot, AddonType, BookingDraft, CommandContext, FoodType, HotelGrade, Intent,
                     Meal, NLUResult, Slots, TourStyleType, TourTheme)
from app.tour_catalog import Tour
from openai import OpenAI, OpenAIError


SYSTEM_PROMPT = """너는 여행 예약 도우미의 문장 해석기야. 고객 말에서 의도와 예약 항목을 뽑아 정해진 JSON으로만 답해.

규칙
- '고객 말' 줄만 해석한다. 그 줄에서 실제로 말한 것만 채우고, 말하지 않은 칸은 null, 빈 목록, false로 둔다.
- '지금까지 채운 예약'과 '최근 고객 말'은 뜻을 이해하는 데만 쓰고, 거기 있는 값을 다시 적지 않는다.
- 투어 목록의 스타일을 보고 고객이 말하지 않은 스타일을 고르지 않는다.
- id를 지어내지 않는다. pastBookingId만 '지난 예약' 목록에 있는 id를 쓴다.

intent
- EDIT_BOOKING: 투어, 인원, 스타일, 호텔, 식사, 커피/샴페인, 출발일을 말하거나 질문에 예/아니요로 답함 ('네', '아니요' 한마디도 EDIT_BOOKING)
- SUBMIT_BOOKING: 이대로 예약하겠다고 함
- REBOOK: 지난 여행과 똑같이(다시) 예약하고 싶다고 함
- CHECK_PRICE: 금액, 가격을 물음
- SEARCH_TOUR: 어떤 투어가 있는지 물음
- VIEW_HISTORY: 지난 여행 기록을 보고 싶다고 함
- UNKNOWN: 예약과 관계없는 말, 이미 한 예약의 취소나 변경 요청

slots
- tourTheme: 허니문/신혼=HONEYMOON, 효도/부모님=PARENT, 골프=GOLF, 트레킹/등산=TREKKING
- tourName: 투어 목록의 이름을 말했으면 목록에 적힌 이름 그대로
- participantsCount: 가는 사람 수 (예: 부모님과 나 = 3)
- tourStyle: 클래식=CLASSIC, 그랜드=GRAND, 프리미엄=PREMIUM. '프리미엄' 한마디는 스타일이다 (뒤에 식사, 레스토랑이 붙을 때만 식사)
- hotelGrade: 3성=_3_STAR, 4성=_4_STAR, 5성=_5_STAR
- foodType: 도시락=LUNCHBOX, 현지식=LOCAL_RESTAURANT, 스테이크/고급 식사=PREMIUM_RESTAURANT
- startDate: 출발 날짜 YYYY-MM-DD. '오늘' 줄의 날짜와 요일을 기준으로 계산하고, 연도 없이 말한 날짜가 지났으면 내년. 요일로 말하면 달력에서 찾는다. 다음 주는 오늘이 있는 주(월~일)의 다음 주
- addons: 커피/샴페인을 넣을 일차(nthDays, 1부터)와 끼니(meals). '매일'이면 allDays=true. '마지막 날'처럼 숫자가 아닌 일차는 지금까지 채운 예약의 투어 일수로 계산하고, 투어가 정해지지 않았으면 nthDays를 비운다
- noAddons: 커피/샴페인을 넣지 않겠다고 함
- useDefaults: '나머지는 기본으로'처럼 남은 항목을 기본값으로 하겠다고 함
- answer: 직전 질문에 대한 대답이면 YES 또는 NO
- transportRequested: 차량을 바꾸거나 고르고 싶다고 함
- pastBookingId: 지난 예약 중 하나를 골라 다시 가고 싶다고 하면 그 예약의 id
- confidence: 해석이 확실하면 0.9 이상, 애매하면 0.5 이하

예시 (지금까지 채운 예약에 효도 투어, 4명이 있을 때)
- 고객 말 "프리미엄" -> tourStyle=PREMIUM 하나만. 테마, 인원은 다시 적지 않음
- 고객 말 "프리미엄 식사로" -> foodType=PREMIUM_RESTAURANT 하나만. tourStyle은 null
- 고객 말 "마지막 날 저녁에 샴페인" -> 효도 투어가 4일이므로 addons=[CHAMPAGNE, nthDays [4], meals [DINNER]] 하나만
예시 (아무것도 정하지 않았을 때)
- 고객 말 "마지막 날 저녁에 샴페인" -> addons=[CHAMPAGNE, nthDays [], meals [DINNER]]
- 고객 말 "효도 여행 5명" -> tourTheme=PARENT, participantsCount=5. tourStyle은 null
"""


THEME_WORDS = {
    r"허니문|신혼": TourTheme.HONEYMOON,
    r"효도|부모님|어르신": TourTheme.PARENT,
    r"골프": TourTheme.GOLF,
    r"트레킹|등산": TourTheme.TREKKING,
}
STYLE_WORDS = {
    r"클래식": TourStyleType.CLASSIC,
    r"그랜드": TourStyleType.GRAND,
    r"프리미엄(?!\s*(식사|레스토랑))": TourStyleType.PREMIUM,
}
HOTEL_WORDS = {
    r"3\s*성": HotelGrade.STAR_3,
    r"4\s*성": HotelGrade.STAR_4,
    r"5\s*성": HotelGrade.STAR_5,
}
FOOD_WORDS = {
    r"도시락": FoodType.LUNCHBOX,
    r"현지식": FoodType.LOCAL_RESTAURANT,
    r"스테이크|고급\s*식사|프리미엄\s*(식사|레스토랑)": FoodType.PREMIUM_RESTAURANT,
}
ADDON_WORDS = {r"커피": AddonType.COFFEE, r"샴페인": AddonType.CHAMPAGNE}
MEAL_WORDS = {r"아침|조식": Meal.BREAKFAST, r"점심|중식": Meal.LUNCH, r"저녁|석식": Meal.DINNER}
WEEKDAYS = "월화수목금토일"
KOREAN_NUMBERS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
DAY_WORDS = {"첫": 1, "둘": 2, "셋": 3, "넷": 4, "다섯": 5, "여섯": 6, "일곱": 7}


def find_value(table: dict, text: str):
    for pattern, value in table.items():
        if re.search(pattern, text):
            return value
    return None


def today_kst() -> date:
    return datetime.now(KST).date()


class IntentParser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        if settings.openai_api_key:
            self._client = OpenAI(api_key=settings.openai_api_key, timeout=10.0, max_retries=1)

    def parse(self, text: str, context: CommandContext, tours: list[Tour]) -> NLUResult:
        if self._client is None:
            return self._parseWithRules(text, tours)
        try:
            response = self._client.chat.completions.parse(
                model=self.settings.openai_model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": self._buildInput(text, context, tours)}],
                response_format=NLUResult,
                temperature=0,
            )
            parsed = response.choices[0].message.parsed
            if parsed is None:
                raise ValueError("OpenAI 응답이 비어 있음")
            return parsed
        except (OpenAIError, ValueError) as error:
            print(f"[IntentParser] OpenAI 실패, 규칙으로 처리: {error}")
        return self._parseWithRules(text, tours)

    def _buildInput(self, text: str, context: CommandContext, tours: list[Tour]) -> str:
        today = today_kst()
        days = [today + timedelta(days=n) for n in range(21)]
        calendar = " ".join(f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})" for d in days)
        lines = [f"오늘: {today.isoformat()} ({WEEKDAYS[today.weekday()]}요일)",
                 f"달력: {calendar}",
                 "", "[참고만 하고 slots에 다시 적지 말 것]", "투어 목록:"]
        for tour in tours:
            styles = ", ".join(style.type for style in tour.styles)
            lines.append(f"- {tour.name} (테마 {tour.tourTheme}, {tour.days}일, 스타일 {styles})")
        draft = context.draft or BookingDraft()
        lines.append("지금까지 채운 예약: " + draft.model_dump_json(exclude_defaults=True))
        names = {tour.id: tour.name for tour in tours}
        for past in context.pastBookings[:5]:
            lines.append(f"지난 예약: id {past.id}, {names.get(past.tourId, '투어')}, 스타일 {past.tourStyleType}, "
                         f"{past.participantsCount}명, 출발 {past.startAt}")
        if context.lastTranscripts:
            lines.append("최근 고객 말: " + " / ".join(context.lastTranscripts[-3:]))
        lines.append("")
        lines.append("[해석할 말]")
        lines.append(f"고객 말: {text}")
        return "\n".join(lines)

    def _parseWithRules(self, text: str, tours: list[Tour]) -> NLUResult:
        slots = Slots(
            tourTheme=find_value(THEME_WORDS, text),
            tourName=self._tourName(text, tours),
            participantsCount=self._people(text),
            tourStyle=find_value(STYLE_WORDS, text),
            hotelGrade=find_value(HOTEL_WORDS, text),
            foodType=find_value(FOOD_WORDS, text),
            startDate=self._date(text),
            addons=self._addons(text),
            noAddons=bool(re.search(r"(커피|샴페인|추가|옵션).*(안\s*(할|해|넣)|없|필요\s*없)", text)),
            useDefaults=bool(re.search(r"기본\s*(으로|값)", text)),
            answer=self._answer(text),
            transportRequested=bool(re.search(r"차량|버스|리무진|차\s*(를|도)?\s*바꿔", text)),
        )
        intent = self._intent(text, slots)
        confidence = 0.7 if intent != Intent.UNKNOWN else 0.3
        return NLUResult(intent=intent, confidence=confidence, slots=slots)

    def _intent(self, text: str, slots: Slots) -> Intent:
        if re.search(r"예약\s*(할게|해\s*줘|해\s*주세요|하겠|진행)", text):
            return Intent.SUBMIT_BOOKING
        if re.search(r"얼마|가격|금액|비용", text):
            return Intent.CHECK_PRICE
        if re.search(r"(지난번|저번|전에).*(똑같이|그대로|다시)", text):
            return Intent.REBOOK
        if re.search(r"(지난|이전|예전)\s*(여행|예약)", text):
            return Intent.VIEW_HISTORY
        if re.search(r"(어떤|무슨)\s*투어|투어\s*(종류|목록|뭐)", text):
            return Intent.SEARCH_TOUR
        if slots.model_dump(exclude_defaults=True):
            return Intent.EDIT_BOOKING
        return Intent.UNKNOWN

    def _tourName(self, text: str, tours: list[Tour]) -> str | None:
        compact = text.replace(" ", "")
        for tour in tours:
            if tour.name.replace(" ", "") in compact:
                return tour.name
        return None

    def _people(self, text: str) -> int | None:
        if re.search(r"혼자", text):
            return 1
        if re.search(r"둘이", text):
            return 2
        match = re.search(r"(\d+|다섯|여섯|일곱|여덟|아홉|열|한|두|세|네)\s*(명|분|사람)", text)
        if match is None:
            return None
        word = match.group(1)
        return int(word) if word.isdigit() else KOREAN_NUMBERS[word]

    def _date(self, text: str) -> date | None:
        today = today_kst()
        if "모레" in text:
            return today + timedelta(days=2)
        if "내일" in text:
            return today + timedelta(days=1)
        match = re.search(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})", text)
        if match:
            return self._safeDate(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        match = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
        if match:
            found = self._safeDate(today.year, int(match.group(1)), int(match.group(2)))
            if found and found < today:
                found = self._safeDate(today.year + 1, found.month, found.day)
            return found
        return None

    def _safeDate(self, year: int, month: int, day: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def _addons(self, text: str) -> list[AddonSlot]:
        addonType = find_value(ADDON_WORDS, text)
        if addonType is None or re.search(r"안\s*(할|해|넣)|빼|없", text):
            return []
        days = [int(n) for n in re.findall(r"(\d+)\s*일\s*차", text)]
        for word, number in DAY_WORDS.items():
            if re.search(word + r"(날|째\s*날)", text):
                days.append(number)
        meals = [meal for pattern, meal in MEAL_WORDS.items() if re.search(pattern, text)]
        return [AddonSlot(addonType=addonType, nthDays=sorted(set(days)), allDays="매일" in text, meals=meals)]

    def _answer(self, text: str) -> str | None:
        stripped = text.strip()
        if re.match(r"(네|예|응|좋아|그래|맞아)요?(?=\s|[.!,~]|$)(?!\s*(명|분|사람))", stripped):
            return "YES"
        if re.match(r"(아니|아뇨|싫어)", stripped):
            return "NO"
        return None
