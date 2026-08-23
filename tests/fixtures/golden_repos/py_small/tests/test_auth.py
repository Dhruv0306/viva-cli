from app.auth.handlers import login

def test_login():
    assert login(None, "alice") == "logged in alice"
