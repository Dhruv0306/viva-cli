"""Stock-level tracking per warehouse."""


def is_low_stock(quantity: int, threshold: int = 5) -> bool:
    """Flag whether a quantity is at or below the reorder threshold."""
    return quantity <= threshold


class StockLevel:
    """Tracks on-hand quantity for one SKU at one warehouse."""

    def __init__(self, sku: str, warehouse: str, quantity: int):
        self.sku = sku
        self.warehouse = warehouse
        self.quantity = quantity

    def reserve(self, amount: int) -> bool:
        """Reserve stock if enough is on hand."""
        if amount > self.quantity:
            return False
        self.quantity -= amount
        return True
