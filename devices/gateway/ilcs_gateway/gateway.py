"""ILCS 网关契约的核心逻辑（与 HTTP 无关，便于单测）。ILCS 侧用 `http_json_v1` 驱动接入。

契约（docs/设备适配器配置模板.md「已内置：HTTPS JSON 网关驱动」）要网关守住的几件事，都在这里：

1. **按指令号去重**：同一指令号再提交，回放台账里的结论，不再调设备；
2. **先落盘再动设备**：见 `ledger.py`；
3. **按指令号查询**：执行器重启后按原指令号问，网关按台账 + 设备状态回答；查不到就 404，不编造；
4. **回执丢了宁可不回**：调设备出了意外（不知道设备动没动）回 `state: unknown`，之后查询再去设备侧找；
5. **保持 / 终止**：控制指令本身也按指令号去重；终止一个已经结束的作业照样确认（设备已在安全状态）；
   启动没拿到应答、设备侧也还没找到的作业，终止回结果未知（不知道它开没开始，也就没法确认停住了），不记成已停；
6. **续跑**：`type: resume` 接续被保持的原作业，新指令号指向同一个作业，不让设备再开一个。

回执的字段与 `api/app/adapters/contract.py` 的解读规则一一对应：command_id、state（accepted / running / done /
failed / unknown）、device_ts、quality（good / bad / uncertain）、delivered、telemetry、error。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import threading
import time
from typing import Any

from .device import Device, Job, ReceiptLost, Rejected, Status
from .ledger import Ledger, TERMINAL

# 启动命令发出后，多久还没在设备侧找到作业就一直报结果未知（交人工核查）
START_TIMEOUT_SEC = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Gateway:
    def __init__(self, device: Device, ledger: Ledger):
        self.device = device
        self.ledger = ledger
        # 同一台设备同一时刻只有一个线程在调它：厂家 SDK 多半不是线程安全的
        self.lock = threading.RLock()
        # 每个指令号让设备真正开始了几次（统一控制口报给接入验收：「重复提交只动作一次」）
        self.starts: Counter[str] = Counter()

    # ---------- 身份 ----------

    def health(self) -> dict[str, Any]:
        with self.lock:
            identity = dict(self.device.identity() or {})
        return {
            **identity, "reachable": True, "version": identity.get("firmware", ""),
            "interlock": bool(identity.get("interlock")), "accepts_commands": bool(identity.get("accepts_commands", True)),
            "simulator": bool(identity.get("simulator")),
            "commands": identity.get("commands") or ["dispatch", "resume", "retry", "hold", "abort", "query"],
        }

    # ---------- 回执 ----------

    @staticmethod
    def receipt(job: dict[str, Any], command_id: str | None = None) -> dict[str, Any]:
        state = job.get("state", "unconfirmed")
        public = {
            "starting": "unknown", "unconfirmed": "unknown", "running": "running", "held": "running",
            "done": "done", "failed": "failed", "rejected": "failed",
        }.get(state, "unknown")
        quality = job.get("quality") or ("bad" if public == "failed" else "uncertain" if public == "unknown" else "good")
        return {
            "command_id": command_id or job["command_id"], "state": public, "phase": state, "device_ts": _now(),
            "quality": quality, "delivered": job.get("delivered") or {}, "telemetry": job.get("telemetry") or [],
            "error": job.get("error") or "",
        }

    @staticmethod
    def _job(record: dict[str, Any]) -> Job:
        return Job(
            command_id=record["command_id"], capability=record.get("capability", ""), params=record.get("params") or {},
            type=record.get("type", "dispatch"), batch_id=record.get("batch_id", ""), step_id=record.get("step_id", ""),
            step_index=int(record.get("step_index") or 0), method=record.get("method") or {},
            handle=record.get("handle", ""),
        )

    # ---------- 提交 ----------

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        command_id = str(body.get("command_id") or "")
        if not command_id:
            raise Rejected("invalid", "缺少 command_id")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise Rejected("invalid", "params 必须是对象")
        with self.lock:
            existing = self.ledger.find(command_id)
            if existing is not None:
                if existing["state"] == "rejected":
                    raise Rejected(existing.get("rejection") or "invalid", existing.get("error") or "设备已拒绝过这条指令")
                return self.receipt(self._refresh(existing), command_id)  # 重复投递：回放，不再动设备
            kind = str(body.get("type") or "dispatch")
            if kind == "resume":
                return self._resume(command_id, body)
            record = {
                "command_id": command_id, "capability": str(body.get("capability") or ""), "params": params,
                "type": kind, "batch_id": str(body.get("batch_id") or ""), "step_id": str(body.get("step_id") or ""),
                "step_index": int(body.get("step_index") or 0), "method": body.get("method") or {},
                "state": "starting", "started_at": time.time(),
            }
            self.ledger.put(record)  # 先落盘：此刻崩掉，重启后也知道这条指令可能已经发给设备
            job = self._job(record)
            try:
                handle = self.device.start(job)
            except Rejected as rejection:
                self.ledger.put({**record, "state": "rejected", "rejection": rejection.kind, "error": rejection.message,
                                 "quality": "good"})
                raise
            except ReceiptLost as lost:
                self.starts[command_id] += 1
                self.ledger.put({**record, "state": "running", "handle": lost.handle or command_id})
                raise
            except Exception as exc:  # noqa: BLE001  不知道设备动没动：记下来，结果未知，绝不重发
                self.ledger.put({**record, "state": "unconfirmed", "error": f"启动时出错，设备是否已动作未知：{exc}"})
                return self.receipt(self.ledger.find(command_id))
            self.starts[command_id] += 1
            self.ledger.put({**record, "state": "running", "handle": handle or command_id})
            return self.receipt(self.ledger.find(command_id))

    def _resume(self, command_id: str, body: dict[str, Any]) -> dict[str, Any]:
        target = str(body.get("target_command_id") or "")
        original = self.ledger.find(target) if target else next(
            (job for job in self.ledger.active() if job.get("state") == "held"
             and job.get("step_id") == body.get("step_id") and job.get("batch_id") == body.get("batch_id")), None,
        )
        if original is None or original.get("state") != "held":
            raise Rejected("invalid", f"没有可接续的被保持作业 {target or '（未指定）'}")
        self.device.resume(self._job(original))
        self.ledger.put({**original, "state": "running"})
        self.ledger.alias(command_id, original["command_id"])
        return self.receipt(self.ledger.find(command_id), command_id)

    # ---------- 查询 ----------

    def query(self, command_id: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.ledger.find(command_id)
            if record is None:
                return None
            return self.receipt(self._refresh(record), command_id)

    def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        """没结束的作业按设备状态推进一次。读不到设备状态就照实报台账里的状态，不猜。"""
        if record.get("control") or record.get("state") in TERMINAL:
            return record
        job = self._job(record)
        if record["state"] in {"starting", "unconfirmed"}:
            try:
                handle = self.device.lookup(job)
            except Exception:  # noqa: BLE001  设备侧也找不到：下次再问
                handle = None
            if not handle:
                if time.time() - float(record.get("started_at") or 0) > START_TIMEOUT_SEC:
                    record = {**record, "state": "unconfirmed",
                              "error": record.get("error") or "启动命令发出后一直没在设备侧找到作业：结果未知，请现场核查"}
                    self.ledger.put(record)
                return record
            record = {**record, "state": "running", "handle": handle, "quality": "uncertain"}
            self.ledger.put(record)
            job = self._job(record)
        try:
            status: Status = self.device.status(job)
        except Exception:  # noqa: BLE001  读不到状态不等于动作失败
            return record
        updated = {**record, "state": status.state, "error": status.error or record.get("error", "")}
        if status.actuals:
            updated["delivered"] = dict(status.actuals)
        if status.telemetry:
            updated["telemetry"] = list(status.telemetry)
        if status.state in {"done", "failed"}:
            updated["finished_at"] = time.time()
        if updated != record:
            self.ledger.put(updated)
        return updated

    # ---------- 保持 / 终止 ----------

    def control(self, kind: str, command_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            existing = self.ledger.find(command_id)
            if existing is not None:
                return self.receipt(existing, command_id)
            target = str(body.get("target_command_id") or "")
            original = self.ledger.find(target) if target else None
            if original is not None:
                original = self._refresh(original)
            if kind == "hold":
                if original is None or original.get("state") != "running":
                    raise Rejected("invalid", f"没有可保持的在途作业 {target or '（未指定）'}")
                self.device.hold(self._job(original))
                self.ledger.put({**original, "state": "held"})
                note = f"作业 {target} 已保持"
            else:
                if original is not None and original.get("state") in {"starting", "unconfirmed"}:
                    # _refresh 刚按指令号在设备侧找过、没找到：不知道它开没开始，拿不到作业号也就停不了它。
                    # 回结果未知、不落控制记录（同一终止指令号再来会重新找一次），原作业也不记成已停
                    return {
                        "command_id": command_id, "state": "unknown", "phase": "unconfirmed", "device_ts": _now(),
                        "quality": "uncertain", "delivered": {}, "telemetry": [],
                        "error": f"作业 {target} 启动没拿到应答、设备侧也还没找到：是否在动作、是否已停住都未知，请现场核查",
                    }
                if original is not None and original.get("state") in {"running", "held"}:
                    self.device.abort(self._job(original))
                    self.ledger.put({**original, "state": "failed", "error": f"被 {command_id} 终止",
                                     "finished_at": time.time()})
                # 目标已经结束或不存在：设备已在安全状态，终止照样确认
                note = "设备已终止并处于安全状态"
            control = {"command_id": command_id, "control": kind, "target": target, "state": "done",
                       "quality": "good", "delivered": {"note": note}, "started_at": time.time()}
            self.ledger.put(control)
            return self.receipt(control, command_id)

    # ---------- 统一控制口（模拟设备） ----------

    def fault_state(self) -> dict[str, Any] | None:
        target = self.device.fault_target()
        if target is None:
            return None
        return {**target.fault_state(), "executions": dict(self.starts), "knows_command_ids": True,
                "motions": int(target.fault_state().get("motions", sum(self.starts.values())))}
