"""A tiny in-memory search index over product names."""


def tokenize(text: str) -> list[str]:
    """Lowercase and split text into simple whitespace tokens."""
    return text.lower().split()


class SearchIndex:
    """Maps tokens to the set of product SKUs that contain them."""

    def __init__(self):
        self._index: dict[str, set[str]] = {}

    def add(self, sku: str, text: str) -> None:
        """Index a product's text under each of its tokens."""
        for token in tokenize(text):
            self._index.setdefault(token, set()).add(sku)

    def search(self, query: str) -> set[str]:
        """Return the SKUs matching every token in the query."""
        results: set[str] | None = None
        for token in tokenize(query):
            hits = self._index.get(token, set())
            results = hits if results is None else results & hits
        return results or set()
