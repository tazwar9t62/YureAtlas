from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def utc(value: str | datetime) -> datetime:
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(UTC)


def jst_day(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), JST).astimezone(UTC)
    return start, start + timedelta(days=1)


def next_day(now: datetime) -> tuple[datetime, datetime]:
    return jst_day(now.astimezone(JST).date() + timedelta(days=1))
