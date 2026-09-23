import uuid

from ..core.db import Base

__all__ = ["Base", "uid"]


def uid() -> str:
    return str(uuid.uuid4())
