from musehost.store import Store


def test_a_new_store_has_the_devices_tokens_and_grants_tables(tmp_path):
    store = Store.open(tmp_path / "musehost.db")
    tables = {
        row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"devices", "tokens", "grants"} <= tables


def test_opening_an_existing_store_keeps_its_data(tmp_path):
    path = tmp_path / "musehost.db"
    store = Store.open(path)
    store.db.execute(
        "INSERT INTO devices (node_id, display_name, enrolled_at) "
        "VALUES ('homelink-abcdef', 'pi', 1)"
    )
    store.db.commit()
    store.close()
    assert Store.open(path).db.execute("SELECT count(*) FROM devices").fetchone()[0] == 1


def test_a_failed_commit_does_not_wedge_the_connection(tmp_path):
    import sqlite3

    import pytest

    from musehost.tokens import _transaction

    path = tmp_path / "locked.db"
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)
    writer.execute("CREATE TABLE t (x)")
    reader = sqlite3.connect(path, isolation_level=None, timeout=0)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t").fetchall()  # holds a shared lock

    with pytest.raises(sqlite3.OperationalError), _transaction(writer):
        writer.execute("INSERT INTO t VALUES (1)")

    reader.execute("COMMIT")
    with _transaction(writer):
        writer.execute("INSERT INTO t VALUES (2)")
    assert writer.execute("SELECT x FROM t").fetchall() == [(2,)]


def test_the_store_uses_wal_and_a_busy_timeout(tmp_path):
    store = Store.open(tmp_path / "musehost.db")
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] > 0


def test_a_database_from_before_superseded_tokens_gains_the_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, node_id TEXT NOT NULL, "
        "vm_id TEXT, issued_at INTEGER NOT NULL, expires_at INTEGER, used_at INTEGER, "
        "rotated_at INTEGER, parent_hash TEXT)"
    )
    old.close()
    columns = {row["name"] for row in Store.open(path).db.execute("PRAGMA table_info(tokens)")}
    assert "superseded_at" in columns
