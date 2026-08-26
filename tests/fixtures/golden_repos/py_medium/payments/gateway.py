"""Payment gateway integration (stubbed, no real network calls)."""


def is_valid_card_number(number: str) -> bool:
    """Very loose card-number shape check for the fixture -- not a real Luhn check."""
    return number.isdigit() and len(number) in (15, 16)


class PaymentGateway:
    """Charges and refunds against a stubbed payment processor."""

    def __init__(self, processor_name: str):
        self.processor_name = processor_name

    def charge(self, amount: float, card_number: str) -> bool:
        """Charge a card if its number passes the basic shape check."""
        return is_valid_card_number(card_number) and amount > 0
