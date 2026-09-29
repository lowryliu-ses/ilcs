"""作业台账：先落盘再动设备。

网关进程任何时刻重启，都要能按原指令号回答「这条指令在设备上怎样了」；同一指令号重复投递要回放原结论，
不能让设备再动一次。所以每一步都先写台账、再调设备：

- 开始动作之前记 `starting`；设备确认开始记 `running`（带设备作业号）；设备明确拒绝记 `rejected`；
- 调用设备时出了意外（不知道设备动没动）记 `unconfirmed`——之后按指令号查询时再去设备侧找，找不到就一直报结果未知；
- 台账写不进去（磁盘满、权限不对）就拒绝工作，不在「忘了自己做过什么」的状态下驱动设备；
- 台账文件损坏同样拒绝工作，不当成空台账继续。

写入是「临时文件 + fsync + 原子替换」，断电也不会留下半个文件；台账文件属主只读（里面有工艺参数）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any

KEEP = 500
TERMINAL = {"done", "failed", "rejected"}


class LedgerError(RuntimeError):
    """台账不可用：网关拒绝驱动设备。"""


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        # 续跑指令号 → 它接续的原作业的指令号
        self.aliases: dict[str, str] = {}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LedgerError(f"作业台账目录 {self.path.parent} 无法创建：{exc}") from exc
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.jobs = dict(raw.get("jobs") or {})
                self.aliases = dict(raw.get("aliases") or {})
            except (OSError, ValueError, AttributeError) as exc:
                raise LedgerError(f"作业台账 {self.path} 损坏：{exc}；不能在没有台账的情况下驱动设备") from exc

    def find(self, command_id: str) -> dict[str, Any] | None:
        with self.lock:
            key = self.aliases.get(command_id, command_id)
            job = self.jobs.get(key)
            return dict(job) if job is not None else None

    def put(self, job: dict[str, Any]) -> None:
        with self.lock:
            self.jobs[job["command_id"]] = {**job, "updated_at": time.time()}
            self._save()

    def alias(self, command_id: str, target: str) -> None:
        with self.lock:
            self.aliases[command_id] = target
            self._save()

    def active(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(job) for job in self.jobs.values() if job.get("state") not in TERMINAL and not job.get("control")]

    def _save(self) -> None:
        if len(self.jobs) > KEEP:
            done = sorted((job for job in self.jobs.values() if job.get("state") in TERMINAL),
                          key=lambda job: job.get("updated_at", 0))
            for job in done[: len(self.jobs) - KEEP]:
                self.jobs.pop(job["command_id"], None)
            live = set(self.jobs)
            self.aliases = {key: value for key, value in self.aliases.items() if value in live}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"jobs": self.jobs, "aliases": self.aliases}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise LedgerError(f"作业台账 {self.path} 写不进去：{exc}；设备不动作") from exc
