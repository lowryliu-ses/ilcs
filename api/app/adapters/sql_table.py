"""数据库中间表驱动（`sql_table_v1`）：设备侧软件与 ILCS 经一个约定好的中间库交换作业。

「中间库 / 中间表」是厂家调度软件、MES、集成商服务常见的对接方式：ILCS 往作业表插一行（`state = 'new'`），
设备侧轮询作业表、执行后回写状态与结果，并在设备表里维持心跳。表结构见 `contracts/sql/ilcs_exchange.sql`。

- 作业表主键是 ILCS 指令号：重投同一指令号只撞主键、回放原结论，设备侧按主键去重；执行器重启后按指令号查回。
- 状态：`new` / `accepted` → 已接受（排队中）；`running` / `held` → 执行中；`done` → 完成；`failed` / `aborted` → 失败；
  `rejected` → 设备明确没动作（失败，带原因）。
- 保持 / 终止写一行 `task_type = hold / abort` 的作业，等设备在 `request_timeout_sec` 内把它改成 done；超时是结果未知。
- 健康检查读设备表：心跳超过 `heartbeat_stale_sec` 不变判失联；`simulator = 1` 的设备正式环境拒绝接入。

```json
{"url": "postgresql+psycopg2://ilcs_exchange@exchange-db.lab.internal:5432/exchange",
 "jobs_table": "ilcs_jobs", "device_table": "ilcs_device", "device_id": "SLR-B-01",
 "heartbeat_stale_sec": 30, "request_timeout_sec": 10, "connect_timeout_sec": 3}
```

口令不写进 URL：`credential_ref`（env:// 或 file://）给口令原文，驱动连接时拼进去。支持 PostgreSQL（psycopg2）、
SQL Server（需在镜像里装 pyodbc 与 ODBC 驱动）、MySQL（需装 pymysql）；SQLite 只用于非正式环境（测试与本机联调）。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlparse

from ..core.config import settings
from .base import (
    AdapterContract, AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest, CommandResult,
)
from .contract import parse_receipt

DRIVER = "sql_table_v1"
DIALECTS = {"postgresql", "mssql", "mysql", "sqlite"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
STATE_MAP = {
    "new": "accepted", "accepted": "accepted", "running": "running", "held": "running", "done": "done",
    "failed": "failed", "aborted": "failed", "rejected": "failed", "unknown": "unknown",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SqlTableAdapter:
    def __init__(self, record):
        from sqlalchemy.engine import make_url

        self.station_id = record.station_id
        self.config = dict(record.config or {})
        raw_url = str(self.config.get("url") or "")
        try:
            url = make_url(raw_url)
        except Exception as exc:
            raise AdapterError("sql_table_v1 的 url 不是有效的 SQLAlchemy 连接串") from exc
        dialect = url.get_backend_name()
        if dialect not in DIALECTS:
            raise AdapterError(f"数据库类型 {dialect} 不支持；可用 {' / '.join(sorted(DIALECTS))}")
        if url.password:
            raise AdapterError("口令不能写进 url：请放 credential_ref（env:// 或 file://）")
        if dialect == "sqlite":
            if settings.environment == "production":
                raise AdapterError("正式环境不能用 SQLite 做中间库")
        elif not settings.adapter_host_allowed(url.host or ""):
            raise AdapterError(f"中间库主机 {url.host or '缺失'} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
        self.url = url
        self.dialect = dialect
        self.jobs = str(self.config.get("jobs_table") or "ilcs_jobs")
        self.devices = str(self.config.get("device_table") or "ilcs_device")
        for table in (self.jobs, self.devices):
            if not IDENTIFIER.fullmatch(table):
                raise AdapterError(f"表名 {table!r} 只能是字母、数字、下划线")
        self.device_id = str(self.config.get("device_id") or self.config.get("expected_device_id") or "")
        if not self.device_id:
            raise AdapterError("sql_table_v1 必须配置 device_id：设备表里哪一行是这台设备")
        self.stale = self._positive("heartbeat_stale_sec", 30.0)
        self.request_timeout = self._positive("request_timeout_sec", 10.0)
        self.connect_timeout = self._positive("connect_timeout_sec", 3.0)
        self.credential_ref = record.credential_ref or ""
        self._engine = None
        self._lock = threading.Lock()
        self.contract = AdapterContract(
            kind="real", protocol=record.protocol or "数据库中间表", version=record.version or "1.0",
            supports_hold=bool(record.supports_hold), supports_abort=bool(record.supports_abort),
            supports_query=bool(record.supports_query), supports_dedup=bool(record.supports_dedup),
            note=record.note or "数据库中间表（ILCS 中间库契约）",
        )

    def _positive(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if value <= 0 or value > 3600:
            raise AdapterError(f"{key} 必须在 0–3600 秒之间")
        return value

    def _password(self) -> str | None:
        reference = self.credential_ref
        if not reference:
            return None
        if reference.startswith("env://"):
            name = reference[len("env://"):]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name not in os.environ:
                raise AdapterError(f"凭据环境变量 {name} 未配置")
            return os.environ[name]
        if reference.startswith("file://"):
            path = Path(urlparse(reference).path).resolve()
            root = Path(settings.adapter_credential_root).resolve()
            if path != root and root not in path.parents:
                raise AdapterError("凭据文件必须位于 ILCS_ADAPTER_CREDENTIAL_ROOT 目录内")
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AdapterError("凭据文件不可读取") from exc
        raise AdapterError("credential_ref 仅支持 env:// 或 file://")

    def _connection(self):
        from sqlalchemy import create_engine

        if self._engine is None:
            url = self.url.set(password=self._password()) if self.credential_ref else self.url
            arguments = {"timeout": self.connect_timeout} if self.dialect == "sqlite" else (
                {"connect_timeout": int(self.connect_timeout)} if self.dialect in {"postgresql", "mysql"} else {"timeout": int(self.connect_timeout)}
            )
            self._engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=0, connect_args=arguments) \
                if self.dialect != "sqlite" else create_engine(url, connect_args=arguments)
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def _run(self, action: str, statement: str, parameters: dict | None = None, *, write: bool = False):
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

        try:
            with self._connection().begin() as connection:
                result = connection.execute(text(statement), parameters or {})
                return result.mappings().all() if not write else result.rowcount
        except IntegrityError:
            raise
        except OperationalError as exc:
            self.close()
            raise AdapterUnreachable(f"中间库{action}没有结论：{exc.__class__.__name__}") from exc
        except SQLAlchemyError as exc:
            # 表不存在、列不对：配置与契约不符，设备侧不会看到这条作业
            raise AdapterError(f"中间库{action}失败（表结构与 ILCS 中间库契约不符？）：{exc.__class__.__name__}: {exc}") from exc

    # ---------- 契约 ----------

    def _device_row(self) -> dict:
        rows = self._run("读设备表", f"SELECT * FROM {self.devices} WHERE device_id = :device_id",
                         {"device_id": self.device_id})
        if not rows:
            raise AdapterUnreachable(f"中间库设备表里没有 {self.device_id}：设备侧软件没有登记心跳")
        return dict(rows[0])

    def identity(self) -> dict:
        row = self._device_row()
        try:
            methods = json.loads(row.get("methods_json") or "null")
        except ValueError:
            methods = None
        identity = {
            "device_id": row.get("device_id"), "model": row.get("model") or "", "vendor": row.get("vendor") or "",
            "firmware": row.get("firmware") or "", "simulator": bool(row.get("simulator")),
            "interlock": bool(row.get("interlock")), "accepts_commands": bool(row.get("accepts_commands", 1)),
            "heartbeat_at": row.get("heartbeat_at"),
        }
        if isinstance(methods, list):
            identity["methods"] = methods
        return identity

    def healthcheck(self) -> dict:
        identity = self.identity()
        if identity["simulator"] and settings.environment == "production":
            raise AdapterError("该设备自报为模拟器；正式环境不接入模拟设备")
        try:
            beat = datetime.fromisoformat(str(identity.get("heartbeat_at") or "").replace("Z", "+00:00"))
        except ValueError as exc:
            raise AdapterUnreachable("设备表的 heartbeat_at 不是 ISO-8601 时间") from exc
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - beat).total_seconds()
        if age > self.stale:
            raise AdapterUnreachable(f"设备侧心跳 {age:.0f} s 没有更新（上限 {self.stale:g} s），设备侧软件可能已停止")
        return {
            "reachable": True, "driver": DRIVER, "protocol": self.contract.protocol, "device_id": identity["device_id"],
            "model": identity["model"], "simulator": identity["simulator"], "interlock": identity["interlock"],
            "accepts_commands": identity["accepts_commands"],
        }

    def _row(self, command_id: str) -> dict | None:
        rows = self._run("查询作业", f"SELECT * FROM {self.jobs} WHERE command_id = :command_id", {"command_id": command_id})
        return dict(rows[0]) if rows else None

    def _receipt(self, row: dict, command_id: str) -> CommandResult:
        state = STATE_MAP.get(str(row.get("state") or ""))
        if state is None:
            raise AdapterIndeterminate(f"中间库作业状态 {row.get('state')!r} 不在契约内")
        try:
            delivered = json.loads(row.get("delivered_json") or "{}")
            telemetry = json.loads(row.get("telemetry_json") or "[]")
        except ValueError as exc:
            raise AdapterIndeterminate("中间库 delivered_json / telemetry_json 不是有效 JSON") from exc
        error = str(row.get("error") or "")
        if row.get("state") == "rejected" and not error:
            error = "设备侧拒绝了这条作业"
        return parse_receipt({
            "command_id": command_id, "state": state,
            "quality": row.get("quality") or ("bad" if state == "failed" else "good"),
            "device_ts": row.get("device_ts") or row.get("updated_at"), "delivered": delivered,
            "telemetry": telemetry, "error": error,
        }, command_id, f"real:{DRIVER}")

    def _insert(self, request: CommandRequest, task_type: str) -> dict:
        from sqlalchemy.exc import IntegrityError

        moment = _now()
        row = {
            "command_id": request.command_id, "station_id": request.station_id, "capability": request.capability,
            "task_type": task_type, "target_command_id": request.target_command_id or None,
            "program": str((request.method or {}).get("program") or "") or None,
            "params_json": json.dumps(request.params or {}, ensure_ascii=False),
            "context_json": json.dumps({"batch_id": request.batch_id, "step_id": request.step_id,
                                        "step_index": request.step_index, "method": request.method or {}},
                                       ensure_ascii=False),
            "state": "new", "created_at": moment, "updated_at": moment,
        }
        try:
            self._run(
                "写作业", f"INSERT INTO {self.jobs} (command_id, station_id, capability, task_type, target_command_id, "
                f"program, params_json, context_json, state, created_at, updated_at) VALUES (:command_id, :station_id, "
                f":capability, :task_type, :target_command_id, :program, :params_json, :context_json, :state, "
                f":created_at, :updated_at)", row, write=True,
            )
        except IntegrityError:
            existing = self._row(request.command_id)  # 重投：主键已存在，回放原作业
            if existing is None:
                raise AdapterIndeterminate("中间库主键冲突，但查不到这条作业")
            return existing
        return {**row, "device_ts": moment}

    def submit(self, request: CommandRequest) -> CommandResult:
        with self._lock:
            existing = self._row(request.command_id)
            row = existing if existing is not None else self._insert(request, request.type)
            return self._receipt(row, request.command_id)

    def query(self, command_id: str) -> CommandResult | None:
        if not self.contract.supports_query:
            return None
        row = self._row(command_id)
        return None if row is None else self._receipt(row, command_id)

    def _control(self, request: CommandRequest) -> CommandResult:
        with self._lock:
            row = self._row(request.command_id) or self._insert(request, request.type)
        deadline = time.monotonic() + self.request_timeout
        while row.get("state") in {"new", "accepted", "running"}:
            if time.monotonic() >= deadline:
                raise AdapterUnreachable(
                    f"{'保持' if request.type == 'hold' else '终止'}请求已写入中间库，{self.request_timeout:g} s 内设备侧没有处理"
                )
            time.sleep(0.2)
            row = self._row(request.command_id) or row
        if row.get("state") == "rejected":
            raise AdapterError(f"设备侧拒绝{'保持' if request.type == 'hold' else '终止'}：{row.get('error') or '无说明'}")
        return self._receipt(row, request.command_id)

    def hold(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_hold:
            raise AdapterError("设备声明不支持保持")
        return self._control(request)

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止")
        return self._control(request)
