from sqlalchemy import inspect, text
from sqlmodel import create_engine

from app import db


def test_startup_migration_adds_new_columns_and_keeps_old_rows(tmp_path, monkeypatch):
    """A database from an early version (game table with few columns) must open and keep its games."""
    old = create_engine(f"sqlite:///{tmp_path}/old.db")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE game (id INTEGER PRIMARY KEY, pgn_raw VARCHAR NOT NULL)"))
        conn.execute(text("INSERT INTO game (id, pgn_raw) VALUES (1, '1. e4 e5 *')"))
    monkeypatch.setattr(db, "engine", old)

    db.create_db_and_tables()
    db.create_db_and_tables()  # running again on every start is harmless

    assert {"coach_cost_usd", "coach_status", "fingerprint", "white"} <= {
        c["name"] for c in inspect(old).get_columns("game")
    }
    with old.connect() as conn:
        row = conn.execute(text("SELECT pgn_raw, white, coach_status, coach_cost_usd FROM game")).one()
    assert tuple(row) == ("1. e4 e5 *", "", "none", 0.0)
