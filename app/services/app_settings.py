"""Settings changed from the UI (stored in the database, unlike .env settings)."""
from sqlmodel import Session

from app.models import AppSetting

MASTERCLASS = "masterclass"


def get_bool(session: Session, key: str, default: bool = False) -> bool:
    row = session.get(AppSetting, key)
    return default if row is None else row.value == "1"


def set_bool(session: Session, key: str, value: bool) -> None:
    row = session.get(AppSetting, key) or AppSetting(key=key, value="0")
    row.value = "1" if value else "0"
    session.add(row)
    session.commit()


def coach_mode(session: Session) -> str:
    return "masterclass" if get_bool(session, MASTERCLASS) else "standard"
