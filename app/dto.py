from pydantic import Field, BaseModel, field_validator
from enum import StrEnum
from datetime import datetime, date, timedelta, timezone
from typing import Literal

KST = timezone(timedelta(hours=9))

# ---enum---

class TourTheme(StrEnum):
    HONEYMOON = "HONEYMOON"
    PARENT = "PARENT"
    GOLF = "GOLF"
    TREKKING = "TREKKING"


class TourStyleType(StrEnum):
    CLASSIC = "CLASSIC"
    GRAND = "GRAND"
    PREMIUM = "PREMIUM"


class HotelGrade(StrEnum):
    STAR_3 = "_3_STAR"
    STAR_4 = "_4_STAR"
    STAR_5 = "_5_STAR"


class FoodType(StrEnum):
    LUNCHBOX = "LUNCHBOX"
    LOCAL_RESTAURANT = "LOCAL_RESTAURANT"
    PREMIUM_RESTAURANT = "PREMIUM_RESTAURANT"


class AddonType(StrEnum):
    COFFEE = "COFFEE"
    CHAMPAGNE = "CHAMPAGNE"


class Meal(StrEnum):
    BREAKFAST = "BREAKFAST"
    LUNCH = "LUNCH"
    DINNER = "DINNER"


# ---BookingDraft---


class BookingAddon(BaseModel):
    addonOptionId: int
    nthDay: int = Field(ge=1)
    meal: Meal


class BookingDraft(BaseModel):
    tourId: int | None = None
    tourStyleId: int | None = None
    hotelOptionId: int | None = None
    foodOptionId: int | None = None
    participantsCount: int | None = Field(default=None, ge=1)
    startAt: datetime | None = None
    endAt: datetime | None = None
    bookingAddons: list[BookingAddon] = Field(default_factory=list)
    @field_validator("startAt", "endAt")
    @classmethod
    def _toKoreanLocalTime(cls, value):
        """시간대가 붙어 오면(+09:00, Z) 한국 시간으로 바꾸고 시간대를 뗀다."""
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(KST).replace(tzinfo=None)
        return value


# ---Enum for Chat---


class Intent(StrEnum):
    EDIT_BOOKING = "EDIT_BOOKING"
    SUBMIT_BOOKING = "SUBMIT_BOOKING"
    REBOOK = "REBOOK"
    CHECK_PRICE = "CHECK_PRICE"
    SEARCH_TOUR = "SEARCH_TOUR"
    VIEW_HISTORY = "VIEW_HISTORY"
    UNKNOWN = "UNKNOWN"

class DraftField(StrEnum):
    tourId = "tourId"
    participantsCount = "participantsCount"
    tourStyleId = "tourStyleId"
    hotelOptionId = "hotelOptionId"
    foodOptionId = "foodOptionId"
    bookingAddons = "bookingAddons"
    startAt = "startAt"
    endAt = "endAt"

class WarningCode(StrEnum):
    TOUR_NOT_FOUND = "TOUR_NOT_FOUND"
    TOUR_NOT_SELECTED = "TOUR_NOT_SELECTED"
    STYLE_NOT_ALLOWED = "STYLE_NOT_ALLOWED"
    OPTION_NOT_AVAILABLE = "OPTION_NOT_AVAILABLE"
    PEOPLE_OUT_OF_RANGE = "PEOPLE_OUT_OF_RANGE"
    DATE_IN_PAST = "DATE_IN_PAST"
    DAY_OUT_OF_RANGE = "DAY_OUT_OF_RANGE"
    TRANSPORT_NOT_CHANGEABLE = "TRANSPORT_NOT_CHANGEABLE"


# ---Command Context(Frontend->AI)---


class PastBooking(BaseModel):
    id: int
    tourId: int | None = None
    tourStyleType: TourStyleType | None = None
    participantsCount: int | None = None
    startAt: datetime | None = None
    endAt: datetime | None = None
    status: str | None = None
    totalPrice: int | None = None

class CommandContext(BaseModel):
    draft: BookingDraft | None = None
    confirmedFields: list[DraftField] = Field(default_factory=list)
    pastBookings: list[PastBooking] = Field(default_factory=list)
    lastTranscripts: list[str] = Field(default_factory=list)

class TextCommandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    context: CommandContext | None = None


# ---Command Response(AI->Frontend)---


class AiWarning(BaseModel):
    code: WarningCode
    message: str


class CommandResponse(BaseModel):
    transcript: str
    intent: Intent
    draft: BookingDraft
    changedFields: list[DraftField] = Field(default_factory=list)
    confirmedFields: list[DraftField] = Field(default_factory=list)  # 다음 요청 context에 그대로 보내면 된다
    missingFields: list[DraftField] = Field(default_factory=list)
    reply: str
    warnings: list[AiWarning] = Field(default_factory=list)


# ---NLUResult---


class AddonSlot(BaseModel):
    addonType: AddonType
    nthDays: list[int] = Field(default_factory=list)
    allDays: bool = False
    meals: list[Meal] = Field(default_factory=list)


class Slots(BaseModel):
    tourTheme: TourTheme | None = None
    tourName: str | None = None
    participantsCount: int | None = None
    tourStyle: TourStyleType | None = None
    hotelGrade: HotelGrade | None = None
    foodType: FoodType | None = None
    startDate: date | None = None
    addons: list[AddonSlot] = Field(default_factory=list)
    noAddons: bool = False
    useDefaults: bool = False
    answer: Literal["YES", "NO"] | None = None
    transportRequested: bool = False
    pastBookingId: int | None = None


class NLUResult(BaseModel):
    intent: Intent
    confidence: float = Field(ge=0, le=1)
    slots: Slots = Field(default_factory=Slots)