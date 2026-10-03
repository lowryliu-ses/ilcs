"""点位写入的请求号台账：`PointAccess.WritePoint` 的 RequestId 去重。

同一个请求号重发回放原结论、不再写；同一个号换了点或值是 RequestConflict。结论（写成了、没写、结果未知）
都记下来，先落盘再回复：宿主重启后重发同一个号，也拿到同一个结论。只留最近的 KEEP 条。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

KEEP = 500


class WriteJournal:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        if path.exists():
            try:
                self.entries = dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                # 台账坏了就说不清哪些请求写过：不能当成空台账继续
                raise RuntimeError(f"点位写入台账 {path} 读不了（{exc}）；请人工核对后再启动") from exc

    def find(self, request_id: str) -> dict | None:
        return self.entries.get(request_id)

    def record(self, request_id: str, entry: dict) -> None:
        self.entries[request_id] = {**entry, "at": time.time()}
        if len(self.entries) > KEEP:
            for stale in sorted(self.entries, key=lambda key: self.entries[key]["at"])[: len(self.entries) - KEEP]:
                self.entries.pop(stale)
        temporary = self.path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self.entries, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
