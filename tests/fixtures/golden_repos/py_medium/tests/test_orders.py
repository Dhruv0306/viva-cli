"""Tests for the orders module's lifecycle transitions."""
from orders.lifecycle import Order, can_transition


def test_pending_to_paid_is_valid():
    assert can_transition("pending", "paid") is True


def test_paid_to_pending_is_invalid():
    assert can_transition("paid", "pending") is False


def test_order_transitions_when_valid():
    order = Order("ord-1")
    order.transition("paid")
    assert order.status == "paid"
