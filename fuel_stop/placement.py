"""Simulated regional conditions and an explicitly bounded placement policy."""
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REGIONS = ("at", "de")
REGION_LABEL = "simulation.fuel-stop/region"
LAST_CHANGE = "simulation.fuel-stop/last-change"
COOLDOWN_SECONDS = 30
MIN_IMPROVEMENT = 0.10


class Environment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_eur_kwh: float = Field(ge=0, le=5, allow_inf_nan=False)
    carbon_g_kwh: float = Field(ge=0, le=2000, allow_inf_nan=False)
    available: bool = True


class Locations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: Environment = Environment(price_eur_kwh=0.30, carbon_g_kwh=80)
    de: Environment = Environment(price_eur_kwh=0.15, carbon_g_kwh=400)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    objective: Literal["carbon", "price"] = "carbon"
    locations: Locations = Field(default_factory=Locations)


@dataclass(frozen=True)
class Decision:
    target: str | None
    reason: str
    wait_seconds: int = 0


def choose(scenario: Scenario, current: str | None, ready_regions: set[str],
           last_change: float, now: float) -> Decision:
    if not scenario.enabled:
        return Decision(None, "Automatic placement paused")
    environments = scenario.locations.model_dump()
    eligible = [r for r in REGIONS if r in ready_regions and environments[r]["available"]]
    if not eligible:
        return Decision(None, "No eligible location; retaining the current deployment")
    key = "carbon_g_kwh" if scenario.objective == "carbon" else "price_eur_kwh"
    # Preserve the current location on ties.
    best = min(eligible, key=lambda r: (environments[r][key], r != current, r))
    if current not in eligible:
        return Decision(best, "Selecting an available location")
    if best == current:
        return Decision(current, "Current location is best")
    current_value, best_value = environments[current][key], environments[best][key]
    if current_value <= 0 or (current_value - best_value) / current_value < MIN_IMPROVEMENT:
        return Decision(current, "Improvement is below the 10% switching threshold")
    wait = max(0, int(COOLDOWN_SECONDS - (now - last_change) + 0.999))
    if wait:
        return Decision(current, "Waiting for the 30-second cooldown", wait)
    return Decision(best, f"Lower simulated {'carbon intensity' if key == 'carbon_g_kwh' else 'electricity price'}")
