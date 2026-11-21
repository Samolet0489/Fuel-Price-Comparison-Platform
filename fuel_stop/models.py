from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Point(Model):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)

    def coordinates(self):
        return [self.lon, self.lat]


class Trip(Model):
    origin: Point
    destination: Point
    fuel_type: Literal["diesel", "petrol95"]
    current_fuel_l: Decimal = Field(ge=0)
    tank_capacity_l: Decimal = Field(gt=0, le=200)
    consumption_l_per_100km: Decimal = Field(gt=0, le=100)
    arrival_reserve_l: Decimal = Field(ge=0)
    minimum_buffer_l: Decimal = Field(default=Decimal("1"), ge=0)
    max_detour_km: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_tank(self):
        if max(self.current_fuel_l, self.arrival_reserve_l, self.minimum_buffer_l) > self.tank_capacity_l:
            raise ValueError("Current fuel, reserve and buffer must fit in the tank")
        return self


class Station(Model):
    id: str
    name: str
    point: Point
    fuel_types: list[str]
    opening_hours: str
    source: str = "OpenStreetMap"


class Route(Model):
    distance_km: Decimal = Field(ge=0)
    duration_s: Decimal = Field(ge=0)
    leg_distances_km: list[Decimal]
    geometry: dict
    attribution: str


class Quote(Model):
    station_id: str
    fuel_type: Literal["diesel", "petrol95"]
    eur_per_l: Decimal = Field(gt=0, le=10)
    observed_at: datetime

    @model_validator(mode="after")
    def timezone_required(self):
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at requires a timezone")
        return self


class Plan(Model):
    status: Literal["direct", "stop", "no_feasible_stop", "prices_unavailable"]
    mode: Literal["live", "replay"]
    route: Route | None = None
    station: Station | None = None
    litres_to_buy: Decimal | None = None
    estimated_cost_eur: Decimal | None = None
    arrival_fuel_l: Decimal | None = None
    detour_km: Decimal | None = None
    price_eur_per_l: Decimal | None = None
    price_timestamp: datetime | None = None
    price_source: str | None = None
    price_retrieved_at: datetime | None = None
    station_source: str | None = None
    station_data_timestamp: datetime | None = None
    stations_compared: int = 0
    feasible_stations: int = 0
    priced_stations: int = 0
    ranking_complete: bool = True
    scope: str = "Public OSM stations with explicit fuel tags and 24/7 opening hours"
    message: str
