from app.core.db import get_connection
from app.auth.handlers import login

def create_app():
    conn = get_connection()
    login(conn, "demo")
    return conn
