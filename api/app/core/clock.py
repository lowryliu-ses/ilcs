"""统一时钟。审计与硬时限都以服务器时间为准，测试可替换。"""
from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_iso() -> str:
    return now().date().isoformat()


def as_utc(value: datetime | None) -> datetime | None:
    """把外部传入的时间统一成库内的无时区 UTC。带偏移的按偏移换算，不带的视为 UTC。"""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
