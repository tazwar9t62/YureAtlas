from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
JST = ZoneInfo("Asia/Tokyo")


def utc(value: str | datetime) -> datetime:
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(UTC)


def jst_day(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), JST).astimezone(UTC)
    return start, start + timedelta(days=1)


def next_day(now: datetime) -> tuple[datetime, datetime]:
    return jst_day(now.astimezone(JST).date() + timedelta(days=1))
