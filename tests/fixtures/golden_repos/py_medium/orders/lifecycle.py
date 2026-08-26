"""Order state-machine transitions."""

VALID_TRANSITIONS = {
    "pending": {"paid", "cancelled"},
    "paid": {"shipped", "refunded"},
    "shipped": {"delivered"},
}


def can_transition(current: str, target: str) -> bool:
    """Check whether an order status transition is allowed."""
    return target in VALID_TRANSITIONS.get(current, set())


class Order:
    """A customer order and its current lifecycle status."""

    def __init__(self, order_id: str):
        self.order_id = order_id
        self.status = "pending"

    def transition(self, target: str) -> None:
        """Move the order to a new status, if the transition is valid."""
        if can_transition(self.status, target):
            self.status = target
