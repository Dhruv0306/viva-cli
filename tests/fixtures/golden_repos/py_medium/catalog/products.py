"""Product catalog lookups and category grouping."""


def normalize_sku(sku: str) -> str:
    """Uppercase and strip a SKU for consistent lookups."""
    return sku.strip().upper()


class Catalog:
    """In-memory product catalog keyed by SKU."""

    def __init__(self):
        self._products: dict[str, dict] = {}

    def add_product(self, sku: str, name: str, category: str) -> None:
        """Register a product under a normalized SKU."""
        self._products[normalize_sku(sku)] = {"name": name, "category": category}

    def find(self, sku: str) -> dict | None:
        """Look up a product by SKU."""
        return self._products.get(normalize_sku(sku))
