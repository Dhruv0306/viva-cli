"""Shipping rate calculation by weight and destination zone."""


def rate_for_zone(weight_kg: float, zone: str) -> float:
    """Compute a shipping rate for a weight and destination zone."""
    base = {"domestic": 5.0, "international": 20.0}.get(zone, 15.0)
    return round(base + weight_kg * 1.5, 2)


class ShippingCalculator:
    """Wraps rate calculation with a configurable carrier surcharge."""

    def __init__(self, carrier_surcharge: float = 0.0):
        self.carrier_surcharge = carrier_surcharge

    def quote(self, weight_kg: float, zone: str) -> float:
        """Return a shipping quote including the carrier surcharge."""
        return rate_for_zone(weight_kg, zone) + self.carrier_surcharge
