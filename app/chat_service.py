"""ChatService: 고객 말을 draft에 반영하고 다음 질문을 만든다.

replyToVoice(): 음성을 SpeechRecognizer로 문장으로 바꾼 뒤 reply()와 같이 처리한다.
reply(): 문장 하나를 처리해 CommandResponse를 돌려준다.
_applyChanges(): IntentParser가 뽑은 뜻(slots)을 TourCatalog로 id로 바꿔 draft에 넣고 규칙을 검사한다.
_askMissing(): 남은 항목 중 최대 2개를 묻는 문장을 만든다.
서버는 대화를 기억하지 않는다. 프론트가 매번 context(draft, 확인된 항목, 지난 예약, 최근 말)를 보낸다.
"""
from datetime import datetime, time

from app.dto import (AddonSlot, AddonType, AiWarning, BookingAddon, BookingDraft, CommandContext,
                     CommandResponse, DraftField, FoodType, HotelGrade, Intent, Meal, NLUResult, PastBooking,
                     TourStyleType, TourTheme, WarningCode)
from app.errors import ApiError
from app.intent_parser import IntentParser, today_kst
from app.speech_recognizer import SpeechRecognizer
from app.tour_catalog import Tour, TourCatalog

CHECKLIST = list(DraftField)[:7]  # 확인받을 7개 (endAt은 출발일로 자동 계산)
GRAND_ONLY = [TourTheme.HONEYMOON, TourTheme.PARENT]  # 그랜드 이상만 고를 수 있는 테마
ALL_MEALS = [Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER]
TOUR_OPTIONS = [DraftField.tourStyleId, DraftField.hotelOptionId, DraftField.foodOptionId,
                DraftField.bookingAddons]  # 투어마다 id가 다른 항목
PRICE_FIELDS = [DraftField.tourId, DraftField.participantsCount, DraftField.tourStyleId,
                DraftField.hotelOptionId, DraftField.foodOptionId]  # 금액 계산에 필요한 항목

# 답 문장에 쓰는 한국어 이름
STYLE_NAMES = {TourStyleType.CLASSIC: "클래식", TourStyleType.GRAND: "그랜드", TourStyleType.PREMIUM: "프리미엄"}
HOTEL_NAMES = {HotelGrade.STAR_3: "3성급 호텔", HotelGrade.STAR_4: "4성급 호텔", HotelGrade.STAR_5: "5성급 호텔"}
FOOD_NAMES = {FoodType.LUNCHBOX: "도시락", FoodType.LOCAL_RESTAURANT: "현지식",
              FoodType.PREMIUM_RESTAURANT: "스테이크"}
ADDON_NAMES = {AddonType.COFFEE: "커피", AddonType.CHAMPAGNE: "샴페인"}
MIN_PEOPLE_NOTICE = ("출발일 2주 전까지 같은 날짜에 함께 가는 인원이 {n}명 이상 모이지 않으면 "
                     "예약이 확정되지 않을 수 있어요.")


def confirm(confirmed: list[DraftField], field: DraftField):
    """확인된 항목에 넣는다 (이미 있으면 그대로)."""
    if field not in confirmed:
        confirmed.append(field)


def unconfirm(confirmed: list[DraftField], field: DraftField):
    """확인된 항목에서 뺀다 (없으면 그대로)."""
    if field in confirmed:
        confirmed.remove(field)


def warn(warnings: list[AiWarning], code: WarningCode, message: str):
    warnings.append(AiWarning(code=code, message=message))


def ro(word: str) -> str:
    """word 뒤에 '로' 또는 '으로'를 붙인다 (받침이 있으면 '으로', ㄹ 받침은 '로')."""
    code = ord(word[-1]) - 0xAC00
    if 0 <= code < 11172 and code % 28 not in (0, 8):
        return word + "으로"
    return word + "로"


def next_questions(missing: list[DraftField]) -> list[DraftField]:
    """다음에 물을 항목: 남은 항목 앞 2개. 스타일을 정하기 전에는 호텔, 식사를 묻지 않는다."""
    asked = missing[:2]
    if DraftField.tourStyleId in asked:
        asked = [field for field in asked if field not in (DraftField.hotelOptionId, DraftField.foodOptionId)]
    return asked


def min_people(tour: Tour) -> int:
    """투어 확정에 필요한 최소 인원 (허니문 2명, 나머지 3명)."""
    return 2 if tour.tourTheme == TourTheme.HONEYMOON else 3


class ChatService:
    def __init__(self, catalog: TourCatalog, parser: IntentParser, recognizer: SpeechRecognizer | None = None):
        self.catalog = catalog
        self.parser = parser
        self.recognizer = recognizer  # 음성을 쓰지 않는 시험에서는 없어도 된다

    def replyToVoice(self, audio: bytes, context: CommandContext | None = None,
                     contentType: str | None = None) -> CommandResponse:
        """녹음한 음성 → 문장(SpeechRecognizer) → reply()와 같은 처리."""
        if self.recognizer is None:
            raise ApiError(503, "STT_UNAVAILABLE", "음성 인식을 쓸 수 없어요. 글로 입력해 주세요.")
        transcript = self.recognizer.transcribe(audio, self.catalog.getAllTours(), contentType)
        if not transcript:
            raise ApiError(422, "NO_SPEECH", "말씀을 알아듣지 못했어요. 다시 말씀해 주세요.")
        return self.reply(transcript, context)

    def reply(self, text: str, context: CommandContext | None = None) -> CommandResponse:
        """문장 하나 → 뜻 파악(IntentParser) → draft 반영(_applyChanges) → 답 문장(_askMissing)."""
        context = context or CommandContext()
        tours = self.catalog.getAllTours()
        nluResult = self.parser.parse(text, context, tours)

        before = context.draft or BookingDraft()
        draft = before.model_copy(deep=True)  # 받은 draft는 두고 복사본을 고친다
        # 확인된 항목은 값이 있는 것만 믿는다 (프론트가 잘못 보내도 요약에서 멈추지 않게)
        confirmedBefore = [field for field in CHECKLIST if field in context.confirmedFields
                           and (field == DraftField.bookingAddons or getattr(before, field) is not None)]
        confirmed = list(confirmedBefore)
        warnings = self._applyChanges(nluResult, draft, confirmed, context.pastBookings)

        changed = [field for field in DraftField if getattr(before, field) != getattr(draft, field)]
        confirmed = [field for field in CHECKLIST if field in confirmed]  # 체크리스트 순서로
        missing = [field for field in CHECKLIST if field not in confirmed]
        intent = nluResult.intent
        if nluResult.slots.answer == "YES" and len(confirmedBefore) == len(CHECKLIST) and not missing:
            intent = Intent.SUBMIT_BOOKING  # 요약 뒤 "이대로 예약할까요?"에 "네"
        # 커피, 샴페인 질문에 "네"만 했으면 몇째 날 어느 끼니인지 다시 묻는다
        askedBefore = next_questions([field for field in CHECKLIST if field not in confirmedBefore])
        wantsAddons = nluResult.slots.answer == "YES" and DraftField.bookingAddons in askedBefore
        tour = self.catalog.findTour(tourId=draft.tourId) if draft.tourId else None
        lines = [warning.message for warning in warnings]
        lines += self._composeReply(intent, draft, tour, changed, missing, wantsAddons, context.pastBookings)
        return CommandResponse(
            transcript=text,
            intent=intent,
            draft=draft,
            changedFields=changed,
            confirmedFields=confirmed,
            missingFields=missing,
            reply=" ".join(lines),
            warnings=warnings,
        )

    # ----- 고객이 말한 것을 draft에 반영 -----

    def _applyChanges(self, nluResult: NLUResult, draft: BookingDraft, confirmed: list[DraftField],
                      pastBookings: list[PastBooking]) -> list[AiWarning]:
        """말한 항목만 draft에 넣고 confirmed에 더한다. 규칙을 어긴 항목은 넣지 않고 경고를 돌려준다."""
        slots = nluResult.slots
        warnings = []

        # 0. 프론트가 보낸 draft를 지금 투어 정보로 다시 검사한다
        self._checkDraft(draft, confirmed, warnings)

        # 1. 직전 질문에 예/아니요 (물어본 것은 next_questions로 다시 계산)
        asked = next_questions([field for field in CHECKLIST if field not in confirmed])
        if slots.answer == "YES":
            for field in asked:
                if field != DraftField.bookingAddons and getattr(draft, field) is not None:
                    confirm(confirmed, field)  # 추천해 둔 호텔, 식사를 그대로 쓰겠다는 뜻
        if slots.answer == "NO" and DraftField.bookingAddons in asked:
            draft.bookingAddons = []
            confirm(confirmed, DraftField.bookingAddons)

        # 2. 투어 (지난 예약과 똑같이, 또는 이름이나 테마로)
        if nluResult.intent == Intent.REBOOK or slots.pastBookingId is not None:
            self._rebook(slots.pastBookingId, pastBookings, draft, confirmed, warnings)
        if slots.tourName or slots.tourTheme:
            tour = self.catalog.findTour(name=slots.tourName, theme=slots.tourTheme)
            if tour is None:
                warn(warnings, WarningCode.TOUR_NOT_FOUND, "말씀하신 투어를 찾지 못했어요.")
            else:
                self._setTour(tour, draft, confirmed)
        tour = self.catalog.findTour(tourId=draft.tourId) if draft.tourId else None

        # 3. 인원
        if slots.participantsCount is not None:
            if slots.participantsCount < 1:
                warn(warnings, WarningCode.PEOPLE_OUT_OF_RANGE, "인원은 1명 이상이어야 해요.")
            else:
                draft.participantsCount = slots.participantsCount
                confirm(confirmed, DraftField.participantsCount)

        # 4. 스타일, 호텔, 식사, addon (투어가 정해져야 id를 찾을 수 있다)
        if tour is None:
            if slots.tourStyle or slots.hotelGrade or slots.foodType or slots.addons:
                warn(warnings, WarningCode.TOUR_NOT_SELECTED, "먼저 어떤 투어로 갈지 정해 주세요.")
        else:
            if slots.tourStyle:
                self._applyStyle(tour, slots.tourStyle, draft, confirmed, warnings)
            if slots.hotelGrade:
                hotel = tour.find_hotel(slots.hotelGrade)
                if hotel is None:
                    warn(warnings, WarningCode.OPTION_NOT_AVAILABLE, "이 투어에는 그 등급의 호텔이 없어요.")
                else:
                    draft.hotelOptionId = hotel.id
                    confirm(confirmed, DraftField.hotelOptionId)
            if slots.foodType:
                food = tour.find_food(slots.foodType)
                if food is None:
                    warn(warnings, WarningCode.OPTION_NOT_AVAILABLE, "이 투어에는 그 식사가 없어요.")
                else:
                    draft.foodOptionId = food.id
                    confirm(confirmed, DraftField.foodOptionId)
            for addon in slots.addons:
                self._applyAddon(tour, addon, draft, confirmed, warnings)

        # 5. addon 없음, 출발일, 차량
        if slots.noAddons:
            draft.bookingAddons = []
            confirm(confirmed, DraftField.bookingAddons)
        if slots.startDate:
            if slots.startDate < today_kst():
                warn(warnings, WarningCode.DATE_IN_PAST, "지난 날짜는 고를 수 없어요.")
            else:
                draft.startAt = datetime.combine(slots.startDate, time(9, 0))  # 시각은 7번에서 투어대로
                confirm(confirmed, DraftField.startAt)
        if slots.transportRequested:
            warn(warnings, WarningCode.TRANSPORT_NOT_CHANGEABLE, "차량은 투어마다 정해져 있어 바꿀 수 없어요.")

        # 6. 나머지는 기본으로
        if slots.useDefaults:
            self._useDefaults(tour, draft, confirmed)

        # 7. 출발, 도착 일시를 투어의 출발, 도착 시각에 맞춘다
        if draft.startAt and tour is not None:
            draft.startAt = tour.start_at(draft.startAt.date())
            draft.endAt = tour.end_at(draft.startAt.date())
        return warnings

    def _checkDraft(self, draft: BookingDraft, confirmed: list[DraftField], warnings: list[AiWarning]):
        """받은 draft에서 지금은 맞지 않는 값(지난 출발일, 없어진 투어나 옵션)을 비우고 확인을 취소한다."""
        if draft.startAt and draft.startAt.date() < today_kst():
            draft.startAt = draft.endAt = None
            unconfirm(confirmed, DraftField.startAt)
            warn(warnings, WarningCode.DATE_IN_PAST, "출발일이 지났어요. 출발 날짜를 다시 골라 주세요.")
        if draft.tourId is None:
            return
        tour = self.catalog.findTour(tourId=draft.tourId)
        if tour is None:
            draft.tourId = None
            unconfirm(confirmed, DraftField.tourId)
            self._clearTourOptions(draft, confirmed)
            warn(warnings, WarningCode.TOUR_NOT_FOUND, "고르셨던 투어를 지금은 예약할 수 없어요. 다른 투어를 골라 주세요.")
            return
        removed = False
        for field, options in [(DraftField.tourStyleId, tour.styles), (DraftField.hotelOptionId, tour.hotelOptions),
                               (DraftField.foodOptionId, tour.foodOptions)]:
            value = getattr(draft, field)
            if value is not None and value not in [option.id for option in options]:
                setattr(draft, field, None)
                unconfirm(confirmed, field)
                removed = True
        addonIds = [option.id for option in tour.addonOptions]
        addons = [a for a in draft.bookingAddons if a.addonOptionId in addonIds and a.nthDay <= tour.days]
        if len(addons) < len(draft.bookingAddons):
            draft.bookingAddons = addons
            unconfirm(confirmed, DraftField.bookingAddons)
            removed = True
        if removed:
            warn(warnings, WarningCode.OPTION_NOT_AVAILABLE, "고르셨던 옵션 중 지금은 고를 수 없는 것을 뺐어요.")

    def _clearTourOptions(self, draft: BookingDraft, confirmed: list[DraftField]):
        """스타일, 호텔, 식사, addon을 비운다 (id가 투어마다 다르므로 투어가 바뀌면 다시 골라야 한다)."""
        draft.tourStyleId = draft.hotelOptionId = draft.foodOptionId = None
        draft.bookingAddons = []
        for field in TOUR_OPTIONS:
            unconfirm(confirmed, field)

    def _setTour(self, tour: Tour, draft: BookingDraft, confirmed: list[DraftField]):
        if tour.id != draft.tourId:
            self._clearTourOptions(draft, confirmed)
        draft.tourId = tour.id
        confirm(confirmed, DraftField.tourId)

    def _applyStyle(self, tour: Tour, styleType: TourStyleType, draft: BookingDraft,
                    confirmed: list[DraftField], warnings: list[AiWarning]):
        if tour.tourTheme in GRAND_ONLY and styleType == TourStyleType.CLASSIC:
            warn(warnings, WarningCode.STYLE_NOT_ALLOWED, f"{tour.name}는 그랜드 이상만 고를 수 있어요.")
            return
        style = tour.find_style(styleType)
        if style is None:
            warn(warnings, WarningCode.OPTION_NOT_AVAILABLE, "이 투어에는 그 스타일이 없어요.")
            return
        draft.tourStyleId = style.id
        confirm(confirmed, DraftField.tourStyleId)
        # 호텔, 식사는 스타일 기본값을 추천해 둔다 (확인은 따로 받음, 고객이 직접 고른 것은 그대로)
        if DraftField.hotelOptionId not in confirmed:
            draft.hotelOptionId = style.hotelOptionId
        if DraftField.foodOptionId not in confirmed:
            draft.foodOptionId = style.foodOptionId

    def _applyAddon(self, tour: Tour, addon: AddonSlot, draft: BookingDraft,
                    confirmed: list[DraftField], warnings: list[AiWarning]):
        option = tour.find_addon(addon.addonType)
        if option is None:
            warn(warnings, WarningCode.OPTION_NOT_AVAILABLE, "이 투어에는 그 추가 옵션이 없어요.")
            return
        if addon.allDays:
            days = list(range(1, tour.days + 1))
        else:
            days = [day for day in addon.nthDays if 1 <= day <= tour.days]
        if not days or len(days) < len(addon.nthDays):
            warn(warnings, WarningCode.DAY_OUT_OF_RANGE,
                 f"{tour.name}는 {tour.days}일 일정이에요. 1~{tour.days}일차 중에서 골라 주세요.")
            if not days:
                return
        meals = addon.meals or ALL_MEALS
        # 같은 종류(커피 또는 샴페인)는 이번에 말한 것으로 바꾼다
        draft.bookingAddons = [item for item in draft.bookingAddons if item.addonOptionId != option.id]
        for day in days:
            for meal in meals:
                draft.bookingAddons.append(BookingAddon(addonOptionId=option.id, nthDay=day, meal=meal))
        confirm(confirmed, DraftField.bookingAddons)

    def _useDefaults(self, tour: Tour | None, draft: BookingDraft, confirmed: list[DraftField]):
        """남은 호텔, 식사는 스타일 기본값, addon은 없음으로 확인한다. 투어, 인원, 스타일, 날짜는 꼭 묻는다."""
        style = None
        if tour is not None:
            style = next((s for s in tour.styles if s.id == draft.tourStyleId), None)
        if style is not None:
            if draft.hotelOptionId is None:
                draft.hotelOptionId = style.hotelOptionId
            if draft.foodOptionId is None:
                draft.foodOptionId = style.foodOptionId
        for field in [DraftField.hotelOptionId, DraftField.foodOptionId]:
            if getattr(draft, field) is not None:  # 스타일에 기본값이 없으면 따로 묻는다
                confirm(confirmed, field)
        confirm(confirmed, DraftField.bookingAddons)

    def _rebook(self, pastBookingId: int | None, pastBookings: list[PastBooking], draft: BookingDraft,
                confirmed: list[DraftField], warnings: list[AiWarning]):
        """지난 예약의 투어, 인원, 스타일을 가져온다. 날짜는 새로 묻는다."""
        past = next((p for p in pastBookings if p.id == pastBookingId), None)
        if past is None and pastBookings:
            past = pastBookings[0]  # 번호를 말하지 않으면 가장 최근 예약
        tour = self.catalog.findTour(tourId=past.tourId) if past and past.tourId else None
        if tour is None:
            warn(warnings, WarningCode.TOUR_NOT_FOUND, "다시 예약할 지난 여행을 찾지 못했어요.")
            return
        self._setTour(tour, draft, confirmed)
        if past.participantsCount:
            draft.participantsCount = past.participantsCount
            confirm(confirmed, DraftField.participantsCount)
        if past.tourStyleType:
            self._applyStyle(tour, past.tourStyleType, draft, confirmed, warnings)

    # ----- 답 문장 -----

    def _composeReply(self, intent: Intent, draft: BookingDraft, tour: Tour | None, changed: list[DraftField],
                      missing: list[DraftField], wantsAddons: bool, pastBookings: list[PastBooking]) -> list[str]:
        """의도에 맞는 답 + 최소 인원 안내 + 다음 질문을 문장 목록으로 만든다."""
        lines = []
        if intent == Intent.SEARCH_TOUR:
            names = ", ".join(f"{t.name}({t.days}일)" for t in self.catalog.getAllTours())
            return lines + [f"예약할 수 있는 투어는 {names}예요. 어떤 투어가 마음에 드세요?"]
        if intent == Intent.VIEW_HISTORY:
            return lines + [self._history(pastBookings)]
        if intent == Intent.UNKNOWN:
            lines.append("여행 예약에 대해 말씀해 주시면 도와드릴게요.")

        # 인원이나 투어가 바뀌었는데 최소 인원보다 적으면 안내
        if tour and draft.participantsCount and (DraftField.participantsCount in changed
                                                 or DraftField.tourId in changed):
            if draft.participantsCount < min_people(tour):
                lines.append(MIN_PEOPLE_NOTICE.format(n=min_people(tour)))

        # 금액 문의: 금액은 프론트가 백엔드에 물어 보여 준다. AI는 짧게 답하고, 빈 칸이 있으면 그것부터 묻는다
        if intent == Intent.CHECK_PRICE:
            empty = [field for field in PRICE_FIELDS if getattr(draft, field) is None]
            if not empty:
                lines.append("선택하신 조건으로 금액을 보여 드릴게요.")
                return lines
            lines.append("금액을 알려 드리려면 먼저 정할 게 있어요.")
            lines.append(self._askMissing(draft, tour, empty, wantsAddons))
            return lines

        if not missing:  # 7개 모두 확인됨
            if intent == Intent.SUBMIT_BOOKING:
                lines.append("예약 페이지로 이동할게요. 내용을 확인하고 [예약하기]를 눌러 주세요.")
            else:
                lines.append(f"{self._summary(draft, tour)}. 이대로 예약할까요?")
            return lines
        if intent == Intent.SUBMIT_BOOKING:
            lines.append("예약하려면 아직 정할 게 있어요.")
        lines.append(self._askMissing(draft, tour, missing, wantsAddons))
        return lines

    def _askMissing(self, draft: BookingDraft, tour: Tour | None, missing: list[DraftField],
                    wantsAddons: bool) -> str:
        """next_questions로 고른 항목(최대 2개)을 묻는다. 호텔, 식사는 스타일 기본값을 추천한다."""
        asked = next_questions(missing)
        hotel = self._hotelName(draft, tour)
        food = self._foodName(draft, tour)
        if asked == [DraftField.hotelOptionId, DraftField.foodOptionId] and hotel and food:
            return f"호텔은 {hotel}, 식사는 {ro(food)} 추천해요. 이대로 할까요?"

        questions = []
        for field in asked:
            if field == DraftField.tourId:
                names = ", ".join(t.name for t in self.catalog.getAllTours())
                questions.append(f"어떤 투어로 가실까요? ({names})")
            elif field == DraftField.participantsCount:
                questions.append("몇 분이 가세요?")
            elif field == DraftField.tourStyleId:
                questions.append(f"스타일은 {self._styleChoices(tour)} 중에서 골라 주세요.")
            elif field == DraftField.hotelOptionId:
                if hotel:
                    questions.append(f"호텔은 {ro(hotel)} 할까요?")
                else:
                    questions.append("호텔은 3성급, 4성급, 5성급 중 어디로 할까요?")
            elif field == DraftField.foodOptionId:
                if food:
                    questions.append(f"식사는 {ro(food)} 할까요?")
                else:
                    questions.append("식사는 도시락, 현지식, 스테이크 중 무엇으로 할까요?")
            elif field == DraftField.bookingAddons:
                if wantsAddons:
                    days = f"1~{tour.days}일차, " if tour else ""
                    questions.append(f"커피나 샴페인을 몇째 날 어느 끼니에 넣을까요? ({days}아침, 점심, 저녁)")
                else:
                    questions.append("커피나 샴페인을 추가할까요?")
            elif field == DraftField.startAt:
                questions.append("출발 날짜는 언제로 할까요?")
        return " ".join(questions)

    def _styleChoices(self, tour: Tour | None) -> str:
        if tour is None:
            return "클래식, 그랜드, 프리미엄"
        styles = [s.type for s in tour.styles]
        if tour.tourTheme in GRAND_ONLY:
            styles = [s for s in styles if s != TourStyleType.CLASSIC]
        return ", ".join(STYLE_NAMES[s] for s in styles)

    def _hotelName(self, draft: BookingDraft, tour: Tour | None) -> str | None:
        if tour is None or draft.hotelOptionId is None:
            return None
        hotel = next((h for h in tour.hotelOptions if h.id == draft.hotelOptionId), None)
        return HOTEL_NAMES[hotel.grade] if hotel else None

    def _foodName(self, draft: BookingDraft, tour: Tour | None) -> str | None:
        if tour is None or draft.foodOptionId is None:
            return None
        food = next((f for f in tour.foodOptions if f.id == draft.foodOptionId), None)
        return FOOD_NAMES[food.type] if food else None

    def _summary(self, draft: BookingDraft, tour: Tour) -> str:
        """예: 효도 힐링 투어, 4명, 프리미엄, 5성급 호텔, 스테이크, 12월 3일 출발, 샴페인 1번"""
        style = next((s for s in tour.styles if s.id == draft.tourStyleId), None)
        parts = [tour.name, f"{draft.participantsCount}명", STYLE_NAMES[style.type] if style else "",
                 self._hotelName(draft, tour) or "", self._foodName(draft, tour) or "",
                 f"{draft.startAt.month}월 {draft.startAt.day}일 출발"]
        for option in tour.addonOptions:
            count = sum(1 for item in draft.bookingAddons if item.addonOptionId == option.id)
            if count:
                parts.append(f"{ADDON_NAMES[option.type]} {count}번")
        if not draft.bookingAddons:
            parts.append("추가 옵션 없음")
        return ", ".join(part for part in parts if part)

    def _history(self, pastBookings: list[PastBooking]) -> str:
        if not pastBookings:
            return "아직 다녀온 여행이 없어요."
        trips = []
        for past in pastBookings[:3]:
            tour = self.catalog.findTour(tourId=past.tourId) if past.tourId else None
            name = tour.name if tour else "투어"
            when = f" {past.startAt.month}월 {past.startAt.day}일" if past.startAt else ""
            trips.append(f"{name}{when} {past.participantsCount or ''}명".strip())
        return "지난 여행은 " + ", ".join(trips) + "이에요. 다음 여행은 어디로 가 볼까요?"
