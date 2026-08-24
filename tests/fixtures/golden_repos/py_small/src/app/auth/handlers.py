from app.core.db import get_connection

def login(conn, username):
    """Pretend to authenticate a user."""
    return f"logged in {username}"
