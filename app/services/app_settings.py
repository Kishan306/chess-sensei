"""Settings changed from the UI (stored in the database, unlike .env settings)."""
from sqlmodel import Session

from app.models import AppSetting

MASTERCLASS = "masterclass"
USERNAMES = "usernames"  # your account names, one per line


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


def clean_usernames(names: list[str]) -> list[str]:
    """Trim, drop empties and case-insensitive duplicates, keep the first spelling."""
    seen, out = set(), []
    for name in names:
        name = name.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def get_usernames(session: Session) -> list[str]:
    """Usernames saved from the UI."""
    row = session.get(AppSetting, USERNAMES)
    return clean_usernames(row.value.splitlines()) if row else []


def set_usernames(session: Session, names: list[str]) -> list[str]:
    names = clean_usernames(names)
    row = session.get(AppSetting, USERNAMES) or AppSetting(key=USERNAMES, value="")
    row.value = "\n".join(names)
    session.add(row)
    session.commit()
    return names


def all_usernames(session: Session, from_env: list[str]) -> list[str]:
    """Every name that counts as you: the ones from the UI plus MY_USERNAMES in .env."""
    return clean_usernames(get_usernames(session) + list(from_env))
