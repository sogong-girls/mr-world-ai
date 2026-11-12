from datetime import date, datetime, time, timedelta
import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.dto import AddonType, FoodType, HotelGrade, TourStyleType, TourTheme

class CatalogUnavailableError(Exception):
    pass

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
                if wanted in tour.name.replace(" ", ""):
                    return tour
        if theme is not None:
            for tour in tours:
                if tour.tourTheme == theme:
                    return tour
        return None

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
