from viva.storage import schema


def test_connect_creates_parent_dir_and_tables(tmp_path):
    db_path = tmp_path / "nested" / "viva.db"
    conn = schema.connect(str(db_path))
    try:
        assert db_path.exists()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"sessions", "qa_records"} <= tables
    finally:
        conn.close()


def test_connect_is_idempotent(tmp_path):
    db_path = tmp_path / "viva.db"
    conn1 = schema.connect(str(db_path))
    conn1.execute(
        "INSERT INTO sessions (session_id, repo_url, status, duration_seconds, "
        "created_at, updated_at) VALUES ('s1', 'url', 'INGESTING', 1800, 'a', 'a')"
    )
    conn1.commit()
    conn1.close()

    # Reconnecting must not wipe existing data (CREATE TABLE IF NOT EXISTS).
    conn2 = schema.connect(str(db_path))
    row = conn2.execute("SELECT session_id FROM sessions WHERE session_id = 's1'").fetchone()
    assert row is not None
    conn2.close()
