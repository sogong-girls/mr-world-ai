import re
from datetime import datetime, time

from app.dto import (AddonSlot, AddonType, AiWarning, BookingAddon, BookingDraft, CommandContext,
                     CommandResponse, DraftField, FoodType, HotelGrade, Intent, Meal, NLUResult, PastBooking,
                     TourStyleType, TourTheme, WarningCode)
from app.errors import ApiError
from app.intent_parser import IntentParser, today_kst
from app.speech_recognizer import SpeechRecognizer
from app.tour_catalog import Tour, TourCatalog

CHECKLIST = list(DraftField)[:7]  
GRAND_ONLY = [TourTheme.HONEYMOON, TourTheme.PARENT] 
ALL_MEALS = [Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER]
TOUR_OPTIONS = [DraftField.tourStyleId, DraftField.hotelOptionId, DraftField.foodOptionId,
                DraftField.bookingAddons]  
PRICE_FIELDS = [DraftField.tourId, DraftField.participantsCount, DraftField.tourStyleId,
                DraftField.hotelOptionId, DraftField.foodOptionId] 

THEME_NAMES = {TourTheme.HONEYMOON: "허니문", TourTheme.PARENT: "효도", TourTheme.GOLF: "골프",
               TourTheme.TREKKING: "트레킹"}
STYLE_NAMES = {TourStyleType.CLASSIC: "클래식", TourStyleType.GRAND: "그랜드", TourStyleType.PREMIUM: "프리미엄"}
HOTEL_NAMES = {HotelGrade.STAR_3: "3성급 숙소", HotelGrade.STAR_4: "4성급 숙소", HotelGrade.STAR_5: "5성급 숙소"}
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
    """경고를 더한다 (커피, 샴페인을 함께 말해 같은 경고가 또 나오면 한 번만)."""
    warning = AiWarning(code=code, message=message)
    if warning not in warnings:
        warnings.append(warning)


def addon_day_question(tour: Tour | None) -> str:
    days = f"1~{tour.days}일차, " if tour else ""
    return f"커피나 샴페인을 몇째 날 어느 끼니에 넣을까요? ({days}아침, 점심, 저녁)"


def has_batchim(word: str) -> bool:
    code = ord(word[-1]) - 0xAC00
    return 0 <= code < 11172 and code % 28 != 0


def eun(word: str) -> str:
    return word + ("은" if has_batchim(word) else "는")


def iga(word: str) -> str:
    return word + ("이" if has_batchim(word) else "가")


def ro(word: str) -> str:
    code = ord(word[-1]) - 0xAC00
    if 0 <= code < 11172 and code % 28 not in (0, 8):
        return word + "으로"
    return word + "로"


def next_questions(missing: list[DraftField]) -> list[DraftField]:
    asked = missing[:2]
    if DraftField.tourStyleId in asked:
        asked = [field for field in asked if field not in (DraftField.hotelOptionId, DraftField.foodOptionId)]
    return asked


def min_people(tour: Tour) -> int:
    return 2 if tour.tourTheme == TourTheme.HONEYMOON else 3


class ChatService:
    def __init__(self, catalog: TourCatalog, parser: IntentParser, recognizer: SpeechRecognizer | None = None):
        self.catalog = catalog
        self.parser = parser
        self.recognizer = recognizer 

    def replyToVoice(self, audio: bytes, context: CommandContext | None = None,
                     contentType: str | None = None) -> CommandResponse:
        if self.recognizer is None:
            raise ApiError(503, "STT_UNAVAILABLE", "음성 인식을 쓸 수 없어요. 글로 입력해 주세요.")
        transcript = self.recognizer.transcribe(audio, self.catalog.getAllTours(), contentType)
        if not transcript:
            raise ApiError(422, "NO_SPEECH", "말씀을 알아듣지 못했어요. 다시 말씀해 주세요.")
        return self.reply(transcript, context)

    def reply(self, text: str, context: CommandContext | None = None) -> CommandResponse:
        context = context or CommandContext()
        tours = self.catalog.getAllTours()
        nluResult = self.parser.parse(text, context, tours)

        before = context.draft or BookingDraft()
        draft = before.model_copy(deep=True)  
        confirmedBefore = [field for field in CHECKLIST if field in context.confirmedFields
                           and (field == DraftField.bookingAddons or getattr(before, field) is not None)]
        confirmed = list(confirmedBefore)
        warnings = self._applyChanges(nluResult, draft, confirmed, context.pastBookings)

        changed = [field for field in DraftField if getattr(before, field) != getattr(draft, field)]
        confirmed = [field for field in CHECKLIST if field in confirmed]  
        missing = [field for field in CHECKLIST if field not in confirmed]
        intent = nluResult.intent
        if nluResult.slots.answer == "YES" and len(confirmedBefore) == len(CHECKLIST) and not missing:
            intent = Intent.SUBMIT_BOOKING  

        askedBefore = next_questions([field for field in CHECKLIST if field not in confirmedBefore])
        wantsAddons = nluResult.slots.answer == "YES" and DraftField.bookingAddons in askedBefore
        tour = self.catalog.findTour(tourId=draft.tourId) if draft.tourId else None
        lines = [warning.message for warning in warnings]
        askedStyles = []  # 투어를 고른 뒤 스타일에 무엇이 들어 있는지 물으면 그 스타일 설명
        if intent == Intent.SEARCH_TOUR and tour is not None:
            if nluResult.slots.tourStyle:
                askedStyles = [nluResult.slots.tourStyle]
            elif re.search(r"스타일|등급", text):
                askedStyles = list(TourStyleType)
        tourChoices = self._tourChoices(nluResult, tour)
        addonDayAsked = any(w.message == addon_day_question(tour) for w in warnings)  # 경고로 일차를 이미 물음
        lines += self._composeReply(intent, draft, tour, changed, missing, wantsAddons, context.pastBookings,
                                    askedStyles, tourChoices, addonDayAsked)
        return CommandResponse(
            transcript=text,
            intent=intent,
            draft=draft,
            changedFields=changed,
            confirmedFields=confirmed,
            missingFields=missing,
            reply=" ".join(line for line in lines if line),
            warnings=warnings,
        )


    def _applyChanges(self, nluResult: NLUResult, draft: BookingDraft, confirmed: list[DraftField],
                      pastBookings: list[PastBooking]) -> list[AiWarning]:
        """말한 항목만 draft에 넣고 confirmed에 더한다. 규칙을 어긴 항목은 넣지 않고 경고를 돌려준다."""
        slots = nluResult.slots
        warnings = []

        self._checkDraft(draft, confirmed, warnings)

        asked = next_questions([field for field in CHECKLIST if field not in confirmed])
        if slots.answer == "YES":
            for field in asked:
                if field != DraftField.bookingAddons and getattr(draft, field) is not None:
                    confirm(confirmed, field)  
        if slots.answer == "NO" and DraftField.bookingAddons in asked:
            draft.bookingAddons = []
            confirm(confirmed, DraftField.bookingAddons)

        if nluResult.intent == Intent.REBOOK or slots.pastBookingId is not None:
            self._rebook(slots.pastBookingId, pastBookings, draft, confirmed, warnings)
        if slots.tourName or slots.tourTheme:
            self._chooseTour(nluResult, draft, confirmed, warnings)
        tour =self.catalog.findTour(tourId=draft.tourId) if draft.tourId else None

        if slots.participantsCount is not None:
            if slots.participantsCount < 1:
                warn(warnings, WarningCode.PEOPLE_OUT_OF_RANGE, "인원은 1명 이상이어야 해요.")
            else:
                draft.participantsCount = slots.participantsCount
                confirm(confirmed, DraftField.participantsCount)

        if nluResult.intent == Intent.SEARCH_TOUR:
            pass  # 물어본 스타일, 호텔, 식사는 고른 것이 아니다
        elif tour is None:
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

        if slots.noAddons and not any(addon.remove for addon in slots.addons):  # 한 종류만 빼는 말이면 나머지는 둔다
            draft.bookingAddons = []
            confirm(confirmed, DraftField.bookingAddons)
        if slots.startDate:
            if slots.startDate < today_kst():
                warn(warnings, WarningCode.DATE_IN_PAST, "지난 날짜는 고를 수 없어요.")
            else:
                draft.startAt = datetime.combine(slots.startDate, time(9, 0))  
                confirm(confirmed, DraftField.startAt)
        if slots.transportRequested:
            warn(warnings, WarningCode.TRANSPORT_NOT_CHANGEABLE, "차량은 투어마다 정해져 있어 바꿀 수 없어요.")

        if slots.useDefaults:
            self._useDefaults(tour, draft, confirmed)

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

    def _chooseTour(self, nluResult: NLUResult, draft: BookingDraft, confirmed: list[DraftField],
                    warnings: list[AiWarning]):
        """말한 투어 이름(여행지로 정해진 이름 포함)이나 테마로 투어를 정한다.

        테마만 말했을 때: 지금 고른 투어가 그 테마면 그대로 두고, 그 테마의 투어가 여럿이면 정하지 않는다
        (어느 투어인지는 답 문장에서 되묻는다). 다른 테마로 바꾸려는 것이면 지금 투어와 옵션을 비운다.
        """
        slots = nluResult.slots
        if slots.tourName:
            tour = self.catalog.findTour(name=slots.tourName)
            if tour is not None:
                self._setTour(tour, draft, confirmed)
                return
        if slots.tourTheme is None:
            warn(warnings, WarningCode.TOUR_NOT_FOUND, "말씀하신 투어를 찾지 못했어요.")
            return
        current = self.catalog.findTour(tourId=draft.tourId) if draft.tourId else None
        if current is not None and current.tourTheme == slots.tourTheme:
            return
        tours = self.catalog.findTours(slots.tourTheme)
        if not tours:
            warn(warnings, WarningCode.TOUR_NOT_FOUND, "말씀하신 투어를 찾지 못했어요.")
        elif len(tours) == 1:
            self._setTour(tours[0], draft, confirmed)
        elif current is not None and nluResult.intent != Intent.SEARCH_TOUR:
            draft.tourId = None
            unconfirm(confirmed, DraftField.tourId)
            self._clearTourOptions(draft, confirmed)

    def _tourChoices(self, nluResult: NLUResult, tour: Tour | None) -> list[Tour]:
        """답에서 보여 줄 투어 후보.

        투어 질문(SEARCH_TOUR): 말한 투어, 또는 말한 테마의 투어 전부 (없으면 빈 목록 → 전체 목록).
        그 밖: 테마만 말해 투어가 정해지지 않았고 그 테마 투어가 여럿이면 그 투어들 (어느 것인지 되묻는다).
        """
        slots = nluResult.slots
        if nluResult.intent == Intent.SEARCH_TOUR:
            if slots.tourName and tour is not None:
                return [tour]
            return self.catalog.findTours(slots.tourTheme) if slots.tourTheme else []
        if slots.tourTheme and (tour is None or tour.tourTheme != slots.tourTheme):
            tours = self.catalog.findTours(slots.tourTheme)
            if len(tours) > 1:
                return tours
        return []

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
        old = next((s for s in tour.styles if s.id == draft.tourStyleId), None)
        styleChanged = old is not None and old.id != style.id
        draft.tourStyleId = style.id
        confirm(confirmed, DraftField.tourStyleId)

        for field in (DraftField.hotelOptionId, DraftField.foodOptionId):
            newDefault = getattr(style, field)
            if styleChanged and getattr(draft, field) == getattr(old, field) and newDefault != getattr(old, field):
                unconfirm(confirmed, field)
            if field not in confirmed:
                setattr(draft, field, newDefault)

    def _applyAddon(self, tour: Tour, addon: AddonSlot, draft: BookingDraft,
                    confirmed: list[DraftField], warnings: list[AiWarning]):
        option = tour.find_addon(addon.addonType)
        if option is None:
            warn(warnings, WarningCode.OPTION_NOT_AVAILABLE, "이 투어에는 그 추가 옵션이 없어요.")
            return
        wanted = list(dict.fromkeys(addon.nthDays))
        if addon.remove:
            self._removeAddon(tour, option.id, addon, wanted, draft, warnings)
            return
        if not addon.allDays and not wanted:  # 일차를 말하지 않으면 넣지 않고 일차를 묻는다
            warn(warnings, WarningCode.DAY_OUT_OF_RANGE, addon_day_question(tour))
            return
        if addon.allDays:
            days = list(range(1, tour.days + 1))
        else:
            days = [day for day in wanted if 1 <= day <= tour.days]
        if not days or len(days) < len(wanted):
            warn(warnings, WarningCode.DAY_OUT_OF_RANGE,
                 f"{tour.name}는 {tour.days}일 일정이에요. 1~{tour.days}일차 중에서 골라 주세요.")
            if not days:
                return
        meals = list(dict.fromkeys(addon.meals)) or ALL_MEALS

        if addon.replace:
            draft.bookingAddons = [item for item in draft.bookingAddons if item.addonOptionId != option.id]
        for day in days:
            for meal in meals:
                item = BookingAddon(addonOptionId=option.id, nthDay=day, meal=meal)
                if item not in draft.bookingAddons:
                    draft.bookingAddons.append(item)
        confirm(confirmed, DraftField.bookingAddons)

    def _removeAddon(self, tour: Tour, optionId: int, addon: AddonSlot, wanted: list[int], draft: BookingDraft,
                     warnings: list[AiWarning]):
        """말한 종류의 addon을 뺀다. 일차·끼니를 말하면 그 칸만, 말하지 않으면 그 종류 전부 (확인 상태는 그대로)."""
        days = [day for day in wanted if 1 <= day <= tour.days]
        if len(days) < len(wanted):
            warn(warnings, WarningCode.DAY_OUT_OF_RANGE,
                 f"{tour.name}는 {tour.days}일 일정이에요. 1~{tour.days}일차 중에서 골라 주세요.")
            if not days:
                return
        if addon.allDays or not wanted:
            days = list(range(1, tour.days + 1))
        meals = addon.meals or ALL_MEALS
        draft.bookingAddons = [item for item in draft.bookingAddons
                               if not (item.addonOptionId == optionId and item.nthDay in days and item.meal in meals)]

    def _useDefaults(self, tour: Tour | None, draft: BookingDraft, confirmed: list[DraftField]):
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
        past = next((p for p in pastBookings if p.id == pastBookingId), None)
        if past is None and pastBookings:
            past = max(pastBookings, key=lambda p: p.startAt.replace(tzinfo=None) if p.startAt else datetime.min)
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
                      missing: list[DraftField], wantsAddons: bool, pastBookings: list[PastBooking],
                      askedStyles: list[TourStyleType] | None = None,
                      tourChoices: list[Tour] | None = None, addonDayAsked: bool = False) -> list[str]:
        """의도에 맞는 답 + 최소 인원 안내 + 다음 질문을 문장 목록으로 만든다."""
        lines = []
        tourChoices = tourChoices or []
        if askedStyles and tour is not None:
            lines.append(self._styleInfo(tour, askedStyles))
        elif intent == Intent.SEARCH_TOUR:
            if tourChoices:
                return lines + [f"{self._choiceList(tourChoices)} 있어요. 어떤 투어가 마음에 드세요?"]
            return lines + [f"예약할 수 있는 투어는 {self._catalogByTheme()}. 어떤 여행이 마음에 드세요?"]
        if intent == Intent.VIEW_HISTORY:
            return lines + [self._history(pastBookings)]
        if intent == Intent.UNKNOWN:
            lines.append("여행 예약에 대해 말씀해 주시면 도와드릴게요.")

        if tour and draft.participantsCount and (DraftField.participantsCount in changed
                                                 or DraftField.tourId in changed):
            if draft.participantsCount < min_people(tour):
                lines.append(MIN_PEOPLE_NOTICE.format(n=min_people(tour)))

        if intent == Intent.CHECK_PRICE:
            empty = [field for field in PRICE_FIELDS if getattr(draft, field) is None]
            if not empty:
                lines.append("선택하신 조건으로 금액을 보여 드릴게요.")
                return lines
            lines.append("금액을 알려 드리려면 먼저 정할 게 있어요.")
            lines.append(self._askMissing(draft, tour, empty, wantsAddons, tourChoices))
            return lines

        if not missing: 
            if intent == Intent.SUBMIT_BOOKING:
                lines.append("예약 페이지로 이동할게요. 내용을 확인하고 [예약하기]를 눌러 주세요.")
            else:
                lines.append(f"{self._summary(draft, tour)}. 이대로 예약할까요?")
            return lines
        if intent == Intent.SUBMIT_BOOKING:
            lines.append("예약하려면 아직 정할 게 있어요.")
        lines.append(self._askMissing(draft, tour, missing, wantsAddons, tourChoices, addonDayAsked))
        return lines

    def _askMissing(self, draft: BookingDraft, tour: Tour | None, missing: list[DraftField],
                    wantsAddons: bool, tourChoices: list[Tour] | None = None, addonDayAsked: bool = False) -> str:
        """next_questions로 고른 항목(최대 2개)을 묻는다. 호텔, 식사는 스타일 기본값을 추천한다.

        투어는 테마부터 묻고(투어 이름 8개를 다 읽어 주면 길다), tourChoices가 있으면(테마의 투어가 여럿)
        그 후보 중에서 묻는다. 스타일은 투어마다 고를 수 있는 것이 달라 투어를 정한 뒤 묻는다.
        """
        asked = next_questions(missing)
        if addonDayAsked:  # 일차 없이 말한 커피, 샴페인은 경고 문장이 이미 물었다
            asked = [field for field in asked if field != DraftField.bookingAddons]
        if DraftField.tourId in asked:
            asked = [field for field in asked if field != DraftField.tourStyleId]
        hotel = self._hotelName(draft, tour)
        food = self._foodName(draft, tour)
        if asked == [DraftField.hotelOptionId, DraftField.foodOptionId] and hotel and food:
            return f"숙소는 {hotel}, 식사는 {ro(food)} 추천해요. 이대로 할까요?"

        questions = []
        for field in asked:
            if field == DraftField.tourId and tourChoices:
                questions.append(f"{self._choiceList(tourChoices)} 있어요. 어느 투어로 가실까요?")
            elif field == DraftField.tourId:
                themes = dict.fromkeys(t.tourTheme for t in self.catalog.getAllTours())
                questions.append(f"어떤 여행을 원하세요? ({', '.join(THEME_NAMES[t] for t in themes)})")
            elif field == DraftField.participantsCount:
                questions.append("몇 분이 가세요?")
            elif field == DraftField.tourStyleId:
                questions.append(f"스타일은 {self._styleChoices(tour)} 중에서 골라 주세요.")
            elif field == DraftField.hotelOptionId:
                if hotel:
                    questions.append(f"숙소는 {ro(hotel)} 할까요?")
                else:
                    questions.append("숙소는 3성급, 4성급, 5성급 중 어디로 할까요?")
            elif field == DraftField.foodOptionId:
                if food:
                    questions.append(f"식사는 {ro(food)} 할까요?")
                else:
                    questions.append("식사는 도시락, 현지식, 스테이크 중 무엇으로 할까요?")
            elif field == DraftField.bookingAddons:
                if wantsAddons:
                    questions.append(addon_day_question(tour))
                else:
                    questions.append("커피나 샴페인을 추가할까요?")
            elif field == DraftField.startAt:
                questions.append("출발 날짜는 언제로 할까요?")
        return " ".join(questions)

    def _tourLabel(self, tour: Tour) -> str:
        """예: 허니문 하와이 선셋 투어(하와이, 6일)"""
        info = ", ".join(part for part in [tour.destination, f"{tour.days}일"] if part)
        return f"{tour.name}({info})"

    def _choiceList(self, tours: list[Tour]) -> str:
        """예: 허니문 투어는 허니문 낭만 투어(유럽, 7일), 허니문 하와이 선셋 투어(하와이, 6일)가 ('있어요' 앞까지)"""
        labels = ", ".join(self._tourLabel(t) for t in tours)
        head = f"{THEME_NAMES[tours[0].tourTheme]} 투어는 " if len({t.tourTheme for t in tours}) == 1 else ""
        josa = iga(tours[-1].name)[-1]  # 괄호 뒤 조사는 괄호 앞 투어 이름에 맞춘다
        return head + labels + josa

    def _catalogByTheme(self) -> str:
        """예: 허니문(유럽 7일, 하와이 6일), 효도(일본 온천 지역 4일, 경주·부산 3일)…이에요 (이름 대신 여행지로 짧게)"""
        groups: dict[TourTheme, list[Tour]] = {}
        for tour in self.catalog.getAllTours():
            groups.setdefault(tour.tourTheme, []).append(tour)
        parts = [f"{THEME_NAMES[theme]}({', '.join(f'{t.destination or t.name} {t.days}일' for t in tours)})"
                 for theme, tours in groups.items()]
        if not parts:
            return "지금 없어요"
        return ", ".join(parts) + "이에요"  # 늘 'N일)'로 끝남

    def _styleInfo(self, tour: Tour, styleTypes: list[TourStyleType]) -> str:
        """예: 효도 힐링 투어의 프리미엄은 5성급 숙소에 스테이크 식사예요. (비교라서 이름 대신 등급, 종류로 짧게)"""
        styles = [s for s in tour.styles if s.type in styleTypes
                  and not (tour.tourTheme in GRAND_ONLY and s.type == TourStyleType.CLASSIC)]
        if not styles:
            return f"{tour.name}에는 그 스타일이 없어요."
        parts = []
        for style in styles:
            hotel = next((h for h in tour.hotelOptions if h.id == style.hotelOptionId), None)
            food = next((f for f in tour.foodOptions if f.id == style.foodOptionId), None)
            what = " ".join(filter(None, [f"{HOTEL_NAMES[hotel.grade]}에" if hotel else "",
                                          FOOD_NAMES[food.type] if food else ""]))
            parts.append(f"{eun(STYLE_NAMES[style.type])} {what}".strip())
        return f"{tour.name}의 " + ", ".join(parts) + " 식사예요."

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
        if hotel is None:
            return None
        return hotel.name or HOTEL_NAMES[hotel.grade]  # 예: 4성급 온천 료칸 (이름이 없으면 4성급 숙소)

    def _foodName(self, draft: BookingDraft, tour: Tour | None) -> str | None:
        if tour is None or draft.foodOptionId is None:
            return None
        food = next((f for f in tour.foodOptions if f.id == draft.foodOptionId), None)
        if food is None:
            return None
        return food.name or FOOD_NAMES[food.type]  # 예: 가이세키 정식 (이름이 없으면 현지식)

    def _summary(self, draft: BookingDraft, tour: Tour) -> str:
        """예: 효도 힐링 투어, 4명, 프리미엄, 5성급 프리미엄 료칸, 스테이크 전문 고급 레스토랑, 12월 3일 출발, 샴페인 1번"""
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
