"""Outbound notification dispatch (email/SMS placeholders)."""


def format_subject(event: str, order_id: str) -> str:
    """Build a notification subject line for an order event."""
    return f"[Order {order_id}] {event}"


class NotificationSender:
    """Dispatches notifications through a pluggable channel."""

    def __init__(self, channel: str):
        self.channel = channel
        self.sent: list[str] = []

    def send(self, subject: str, body: str) -> None:
        """Send a notification and record it for later inspection."""
        self.sent.append(f"{subject}: {body}")
