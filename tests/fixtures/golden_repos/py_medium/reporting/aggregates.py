"""Simple reporting aggregates over orders."""


def total_revenue(order_totals: list[float]) -> float:
    """Sum a list of order totals."""
    return round(sum(order_totals), 2)


def average_order_value(order_totals: list[float]) -> float:
    """Compute the mean order value, or 0 for an empty list."""
    if not order_totals:
        return 0.0
    return round(total_revenue(order_totals) / len(order_totals), 2)


class DailyReport:
    """Accumulates order totals for a single reporting day."""

    def __init__(self, date: str):
        self.date = date
        self.order_totals: list[float] = []

    def add_order(self, total: float) -> None:
        """Record one order's total against this day's report."""
        self.order_totals.append(total)
