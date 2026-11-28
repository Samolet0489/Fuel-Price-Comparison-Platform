"""Deterministic prices for one catalogue revision."""
import os
from decimal import Decimal
from hashlib import sha256


SOURCE = "Generated station prices"
BASE_MILLS = {"diesel": 1350, "petrol95": 1400}


def generated_prices_enabled() -> bool:
    return os.getenv("PRICE_MODE", "generated") == "generated"


def generated_price(station_id: str, fuel_type: str, revision: int) -> Decimal:
    digest = sha256(f"{station_id}:{fuel_type}:{revision}".encode()).digest()
    # Adjacent revisions use opposite last-digit parity so every price changes.
    mills = BASE_MILLS[fuel_type] + 2 * (int.from_bytes(digest[:4], "big") % 250) + revision % 2
    return Decimal(mills).scaleb(-3)
