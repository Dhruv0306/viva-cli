"""Invoice generation and line-item totals."""


def line_item_total(unit_price: float, quantity: int) -> float:
    """Compute the total for a single invoice line item."""
    return round(unit_price * quantity, 2)


def invoice_total(line_items: list[tuple[float, int]]) -> float:
    """Sum every line item on an invoice."""
    return round(sum(line_item_total(p, q) for p, q in line_items), 2)


class Invoice:
    """A single customer invoice."""

    def __init__(self, customer_id: str):
        self.customer_id = customer_id
        self.line_items: list[tuple[float, int]] = []

    def add_item(self, unit_price: float, quantity: int) -> None:
        """Append a line item to the invoice."""
        self.line_items.append((unit_price, quantity))
