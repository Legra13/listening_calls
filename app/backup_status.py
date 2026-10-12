"""Статус ночного бэкапа базы для баннера админам (см. scripts/backup_db.sh)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "backups"
_MAX_AGE = timedelta(hours=36)  # таймер раз в сутки + запас


def _read(name: str) -> str | None:
    try:
        return (_BACKUP_DIR / name).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def backup_alert() -> str | None:
    """Текст предупреждения, если бэкап сломан или давно не проходил; иначе None."""
    error = _read("backup_error")
    if error:
        return f"Последний бэкап базы не удался: {error}."

    ok = _read("backup_ok")
    if not ok:
        return "Бэкап базы на Google Диск ещё ни разу не прошёл успешно."
    try:
        last = datetime.strptime(ok, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return f"Не удалось прочитать время последнего бэкапа: {ok}."
    if datetime.now(timezone.utc) - last > _MAX_AGE:
        msk = (last + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        return f"Бэкап базы давно не проходил — последний успешный {msk} МСК."
    return None
