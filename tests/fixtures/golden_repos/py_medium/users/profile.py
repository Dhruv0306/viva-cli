"""User profile data and display-name formatting."""


def display_name(first: str, last: str) -> str:
    """Format a user's display name from first/last name parts."""
    return f"{first.strip()} {last.strip()}".strip()


class UserProfile:
    """A user's profile fields."""

    def __init__(self, user_id: str, first: str, last: str, email: str):
        self.user_id = user_id
        self.first = first
        self.last = last
        self.email = email

    def name(self) -> str:
        """Return the user's formatted display name."""
        return display_name(self.first, self.last)
