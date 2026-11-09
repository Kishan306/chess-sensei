from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_url = get_settings().database_url
engine = create_engine(
    _url,
    connect_args={"check_same_thread": False} if _url.startswith("sqlite") else {},
)


def create_db_and_tables() -> None:
    from app import models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all doesn't alter existing tables, so add columns introduced by later phases."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(engine.dialect)
                default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                sql = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                if default is not None:
                    sql += f" DEFAULT {default!r}" if isinstance(default, str) else f" DEFAULT {default}"
                conn.execute(text(sql))


def get_session():
    with Session(engine) as session:
        yield session


def backfill_fingerprints() -> None:
    """Games uploaded before duplicate detection existed get their fingerprint now."""
    from sqlmodel import select

    from app.models import Game
    from app.services import pgn_service

    with Session(engine) as session:
        for game in session.exec(select(Game).where(Game.fingerprint == None)):  # noqa: E711
            parsed, _ = pgn_service.parse_pgn_text(game.pgn_raw)
            if parsed:
                game.fingerprint = pgn_service.fingerprint(parsed[0].headers, parsed[0].sans)
                game.source_url = pgn_service.source_url(parsed[0].headers)
                session.add(game)
        session.commit()


def reset_interrupted_analyses() -> None:
    """Games left 'running' by a previous server process will never finish; mark them failed."""
    from sqlmodel import select

    from app.models import Game

    with Session(engine) as session:
        for game in session.exec(select(Game).where(Game.analysis_status == "running")):
            game.analysis_status = "error"
            game.analysis_error = "Interrupted (server restarted). Click Analyse to retry."
            session.add(game)
        for game in session.exec(select(Game).where(Game.coach_status == "running")):
            game.coach_status = "error"
            game.coach_error = "Interrupted (server restarted). Try again."
            session.add(game)
        session.commit()
