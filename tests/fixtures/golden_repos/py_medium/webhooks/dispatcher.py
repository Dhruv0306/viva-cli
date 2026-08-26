"""Outbound webhook dispatch to registered subscriber URLs."""


def is_valid_url(url: str) -> bool:
    """Very loose URL shape check for the fixture."""
    return url.startswith("http://") or url.startswith("https://")


class WebhookDispatcher:
    """Fans an event out to every subscriber registered for it."""

    def __init__(self):
        self._subscribers: dict[str, list[str]] = {}

    def subscribe(self, event: str, url: str) -> bool:
        """Register a subscriber URL for an event, if the URL looks valid."""
        if not is_valid_url(url):
            return False
        self._subscribers.setdefault(event, []).append(url)
        return True
