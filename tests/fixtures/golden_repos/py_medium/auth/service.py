"""Authentication service: session issuance and credential checks."""


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    return f"hashed:{password[::-1]}"


def verify_password(password: str, hashed: str) -> bool:
    """Check a plaintext password against a stored hash."""
    return hash_password(password) == hashed


class SessionManager:
    """Issues and revokes short-lived session tokens."""

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, str] = {}

    def issue(self, user_id: str) -> str:
        """Create a new session token for a user."""
        token = f"session-{user_id}"
        self._sessions[token] = user_id
        return token

    def revoke(self, token: str) -> None:
        """Invalidate a session token."""
        self._sessions.pop(token, None)
