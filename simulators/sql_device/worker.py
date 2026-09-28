"""ILCS 中间库模拟设备：模拟「厂家调度软件轮询中间库作业表」的设备侧，系统侧用 `sql_table_v1` 接入。

表结构按 `contracts/sql/ilcs_exchange.sql`（没有就建）。每个周期：
1. 更新设备表的心跳（`heartbeat_at`）、联锁、是否接受作业；
2. 取 `state = 'new'` 的作业：动作作业交给设备模型执行（按主键去重），保持 / 终止作业处理后改成 done；
   设备明确拒绝的改成 `rejected` 并写原因；
3. 在途作业按设备模型的进度回写 `state`、`delivered_json`、`telemetry_json`、`device_ts`。

故障注入：设备表的 `sim_fault` 列写「模式 参数」（`fault.py` 就是这么做的）；`offline` 让它停止处理与心跳 N 秒。

    python simulators/sql_device/worker.py --url postgresql+psycopg2://exchange@sql-sim-exchange:5432/exchange \\
        --device-id SIM-SLR-B --profile generic
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulators.common.device import DeviceRejected, ReceiptLost, SimulatedDevice  # noqa: E402
from simulators.common.runtime import build_device, configure_logging, device_arguments  # noqa: E402

MARK = "ILCS-SIMULATOR"
log = logging.getLogger("ilcs.sql-sim")


def _now(device: SimulatedDevice | None = None) -> str:
    moment = device.now() if device is not None else datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds")


def schema(jobs: str = "ilcs_jobs", devices: str = "ilcs_device"):
    from sqlalchemy import Column, Index, Integer, MetaData, String, Table, Text

    metadata = MetaData()
    Table(jobs, metadata,
          Column("command_id", String(64), primary_key=True), Column("station_id", String(64), nullable=False),
          Column("capability", String(64), nullable=False), Column("task_type", String(16), nullable=False),
          Column("target_command_id", String(64)), Column("program", String(128)),
          Column("params_json", Text, nullable=False), Column("context_json", Text, nullable=False),
          Column("state", String(16), nullable=False), Column("quality", String(16)), Column("delivered_json", Text),
          Column("telemetry_json", Text), Column("error", Text), Column("device_ts", String(40)),
          Column("created_at", String(40), nullable=False), Column("updated_at", String(40), nullable=False),
          Index(f"{jobs}_state", "state", "created_at"))
    Table(devices, metadata,
          Column("device_id", String(64), primary_key=True), Column("model", String(128)), Column("vendor", String(128)),
          Column("firmware", String(64)), Column("heartbeat_at", String(40)), Column("interlock", Integer, default=0),
          Column("accepts_commands", Integer, default=1), Column("simulator", Integer, default=0),
          Column("methods_json", Text), Column("sim_fault", String(64)))
    return metadata


class SqlDeviceWorker:
    def __init__(self, url: str, device: SimulatedDevice, *, jobs: str = "ilcs_jobs", devices: str = "ilcs_device",
                 poll_seconds: float = 0.2):
        from sqlalchemy import create_engine

        self.device = device
        self.jobs, self.devices = jobs, devices
        self.poll = poll_seconds
        self.engine = create_engine(url)
        self.metadata = schema(jobs, devices)
        self.metadata.create_all(self.engine)
        self.stop_event = threading.Event()
        self.offline_until = 0.0
        self.last_fault = ""
        self._register()

    def _register(self) -> None:
        from sqlalchemy import delete, insert

        table = self.metadata.tables[self.devices]
        with self.engine.begin() as connection:
            connection.execute(delete(table).where(table.c.device_id == self.device.device_id))
            connection.execute(insert(table).values(
                device_id=self.device.device_id, model=self.device.model, vendor=MARK, firmware=self.device.firmware,
                heartbeat_at=_now(), interlock=0, accepts_commands=1, simulator=1,
                methods_json=json.dumps(self.device.methods, ensure_ascii=False), sim_fault="",
            ))

    # ---------- 一个周期 ----------

    def cycle(self) -> None:
        from sqlalchemy import select, update

        devices, jobs = self.metadata.tables[self.devices], self.metadata.tables[self.jobs]
        with self.engine.begin() as connection:
            fault = connection.execute(select(devices.c.sim_fault).where(
                devices.c.device_id == self.device.device_id)).scalar() or ""
        if fault != self.last_fault:
            self.last_fault = fault
            mode, _, parameter = fault.partition(" ")
            if mode == "offline":
                self.offline_until = time.monotonic() + (float(parameter or 0) or 5)
            elif mode:
                try:
                    self.device.set_fault(mode, float(parameter or 0))
                except ValueError:
                    log.warning("未知故障模式 %s", mode)
        if time.monotonic() < self.offline_until:
            return  # 设备侧软件停了：不处理作业，也不更新心跳
        self.device.tick()
        with self.engine.begin() as connection:
            connection.execute(update(devices).where(devices.c.device_id == self.device.device_id).values(
                heartbeat_at=_now(), interlock=int(self.device.interlock),
                accepts_commands=int(self.device.fault != "busy"),
            ))
            pending = connection.execute(select(jobs).where(jobs.c.state == "new").order_by(jobs.c.created_at)).mappings().all()
        for row in pending:
            self._take(dict(row))
        with self.engine.begin() as connection:
            active = connection.execute(select(jobs).where(jobs.c.state.in_(["accepted", "running", "held"]))
                                        ).mappings().all()
        for row in active:
            self._progress(dict(row))

    def _write(self, command_id: str, **values) -> None:
        from sqlalchemy import update

        jobs = self.metadata.tables[self.jobs]
        with self.engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.command_id == command_id).values(updated_at=_now(), **values))

    def _take(self, row: dict) -> None:
        command_id, kind = row["command_id"], row["task_type"]
        try:
            if kind in {"hold", "abort"}:
                action = self.device.hold if kind == "hold" else self.device.abort
                receipt = action(command_id, row.get("target_command_id") or "")
                self._write(command_id, state="done", quality="good", device_ts=receipt["device_ts"],
                            delivered_json=json.dumps(receipt.get("delivered") or {}, ensure_ascii=False))
                return
            context = json.loads(row.get("context_json") or "{}")
            receipt = self.device.submit(command_id, kind, row["capability"], json.loads(row["params_json"] or "{}"),
                                         context)
        except DeviceRejected as error:
            self._write(command_id, state="rejected", quality="bad", error=f"{error.identifier}：{error.message}",
                        device_ts=_now(self.device))
            return
        except ReceiptLost:
            return  # 设备已动作但没来得及回写：下一个周期按主键查设备模型再写（设备模型按指令号去重）
        self._apply(command_id, receipt)

    def _progress(self, row: dict) -> None:
        receipt = self.device.query(row["command_id"])
        if receipt.get("state") != "not_found":
            self._apply(row["command_id"], receipt)

    def _apply(self, command_id: str, receipt: dict) -> None:
        phase = receipt.get("phase") or receipt["state"]
        state = {"accepted": "accepted", "running": "running", "held": "held", "done": "done",
                 "failed": "failed", "aborted": "aborted"}.get(phase, receipt["state"])
        self._write(command_id, state=state, quality=receipt.get("quality") or "good", device_ts=receipt["device_ts"],
                    delivered_json=json.dumps(receipt.get("delivered") or {}, ensure_ascii=False),
                    telemetry_json=json.dumps(receipt.get("telemetry") or [], ensure_ascii=False),
                    error=receipt.get("error") or "")

    # ---------- 运行 ----------

    def run(self) -> None:
        while not self.stop_event.wait(self.poll):
            try:
                self.cycle()
            except Exception:
                log.exception("中间库轮询失败")

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        self.engine.dispose()


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    device_arguments(parser, default_port=0)
    parser.add_argument("--url", default=env("SIM_DATABASE_URL", "sqlite:///./exchange.db"))
    parser.add_argument("--jobs-table", default=env("SIM_JOBS_TABLE", "ilcs_jobs"))
    parser.add_argument("--device-table", default=env("SIM_DEVICE_TABLE", "ilcs_device"))
    parser.add_argument("--poll-seconds", type=float, default=float(env("SIM_POLL_SECONDS", "0.5")))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    args = parse(argv)
    device = build_device(args)
    for _ in range(30):  # 数据库容器可能还没起来
        try:
            worker = SqlDeviceWorker(args.url, device, jobs=args.jobs_table, devices=args.device_table,
                                     poll_seconds=args.poll_seconds)
            break
        except Exception as exc:
            log.warning("中间库还连不上（%s），稍后重试", exc.__class__.__name__)
            time.sleep(2)
    else:
        return 2
    log.info("中间库模拟设备 %s 已启动：%s", args.device_id, args.url.split("@")[-1])
    import signal

    signal.signal(signal.SIGTERM, lambda *_: worker.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop_event.set())
    worker.run()
    worker.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
