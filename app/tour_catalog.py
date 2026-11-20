import re
from datetime import date, datetime, time, timedelta
import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.dto import AddonType, FoodType, HotelGrade, TourStyleType, TourTheme

class CatalogUnavailableError(Exception):
    pass


PLACE_SKIP = {"지역", "인근"}  # 여행지에 붙어 있지만 장소를 가리키지 않는 말

class TourStyle(BaseModel):
    id: int
    type: TourStyleType
    hotelOptionId: int | None = None
    foodOptionId: int | None = None


class HotelOption(BaseModel):
    id: int
    name: str = ""
    grade: HotelGrade


class FoodOption(BaseModel):
    id: int
    name: str = ""
    type: FoodType


class AddonOption(BaseModel):
    id: int
    type: AddonType


class Tour(BaseModel):
    id: int
    name: str
    destination: str = ""
    tourTheme: TourTheme
    days: int
    departureTime: time
    arrivalTime: time
    styles: list[TourStyle] = Field(default_factory=list)
    hotelOptions: list[HotelOption] = Field(default_factory=list)
    foodOptions: list[FoodOption] = Field(default_factory=list)
    addonOptions: list[AddonOption] = Field(default_factory=list)

    def find_style(self, styleType: TourStyleType) -> TourStyle | None:
        for style in self.styles:
            if style.type == styleType:
                return style
        return None

    def find_hotel(self, grade: HotelGrade) -> HotelOption | None:
        for hotel in self.hotelOptions:
            if hotel.grade == grade:
                return hotel
        return None

    def find_food(self, foodType: FoodType) -> FoodOption | None:
        for food in self.foodOptions:
            if food.type == foodType:
                return food
        return None

    def find_addon(self, addonType: AddonType) -> AddonOption | None:
        for addon in self.addonOptions:
            if addon.type == addonType:
                return addon
        return None

    def place_words(self) -> list[str]:
        """여행지에서 고객이 말할 만한 낱말 (예: '경주·부산' → 경주, 부산 / '제주도' → 제주도, 제주)."""
        words = []
        for word in re.split(r"[\s·,/]+", self.destination):
            if len(word) < 2 or word in PLACE_SKIP:
                continue
            words.append(word)
            if len(word) >= 3 and word.endswith("도"):
                words.append(word[:-1])
        return words

    def mentions_place(self, text: str) -> bool:
        compact = text.replace(" ", "")
        return any(word in compact for word in self.place_words())

    def start_at(self, startDate: date) -> datetime:
        return datetime.combine(startDate, self.departureTime)

    def end_at(self, startDate: date) -> datetime:
        return datetime.combine(startDate + timedelta(days=self.days - 1), self.arrivalTime)


class TourCatalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._tours: list[Tour] = []
        self._fetchedAt: datetime | None = None
        self.backendStatus = "unknown"

    def getAllTours(self) -> list[Tour]:
        if self._tours and self._isFresh():
            return self._tours
        try:
            self._tours = self._fetchFromBackend()
            self._fetchedAt = datetime.now()
            self.backendStatus = "ok"
            return self._tours
        except (httpx.HTTPError, ValueError) as error:
            self.backendStatus = "error"
            print(f"[TourCatalog] 백엔드에서 투어를 받지 못함: {error}")
            if self._tours:
                return self._tours
            raise CatalogUnavailableError("투어 정보를 불러올 수 없어요. 잠시 후 다시 시도해 주세요.") from error

    def findTour(self, tourId: int | None = None, theme: TourTheme | None = None,
                 name: str | None = None) -> Tour | None:
        tours = self.getAllTours()
        if tourId is not None:
            for tour in tours:
                if tour.id == tourId:
                    return tour
        if name:
            wanted = name.replace(" ", "")
            for tour in tours:
                if wanted == tour.name.replace(" ", ""):
                    return tour
            # 이름 일부만 말했으면 투어 하나에만 들어맞을 때 그 투어 ('허니문'처럼 여럿에 맞으면 못 찾음)
            partial = [tour for tour in tours if wanted in tour.name.replace(" ", "")]
            if len(partial) == 1:
                return partial[0]
        if theme is not None:
            for tour in tours:
                if tour.tourTheme == theme:
                    return tour
        return None

    def findTours(self, theme: TourTheme) -> list[Tour]:
        """그 테마의 투어 전부 (한 테마에 투어가 여럿일 수 있어 고객에게 되물을 때 씀)."""
        return [tour for tour in self.getAllTours() if tour.tourTheme == theme]

    def _isFresh(self) -> bool:
        age = datetime.now() - self._fetchedAt
        return age.total_seconds() < self.settings.catalog_ttl_sec

    def _fetchFromBackend(self) -> list[Tour]:
        base = self.settings.backend_base_url
        tours = []
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f"{base}/api/tours")
            response.raise_for_status()
            for item in response.json():
                detail = client.get(f"{base}/api/tours/{item['id']}")
                detail.raise_for_status()
                try:
                    tours.append(Tour.model_validate(detail.json()))
                except ValidationError as error:
                    print(f"[TourCatalog] 투어 {item['id']}는 정보가 모자라 뺌: {error.error_count()}개 칸")
        return tours
