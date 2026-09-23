"""执行器进程。与业务后端共享领域模型，但生命周期独立：设备侧重启不拖垮 API。

它做三件事：
1. 领取待投递的设备命令，交给对应适配器（`app/adapters/`）；
2. 重启对账：可能已发出但未确认的命令先按原 command_id 查设备侧状态，
   查不到就留在「结果未知」转人工核查，不生成新命令盲目重试；
3. 推进流程：处理到期的等待步骤与待处理事件，所以没有浏览器请求也能往前走。

它不再建表、不再补列、不再播种——迁移是部署的独立步骤，库版本不兼容就直接退出，
不在一个自己看不懂的结构上读写。
"""
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(API_DIR))
os.chdir(API_DIR)

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import ADVISORY_NAMESPACE, SessionLocal, engine  # noqa: E402
from app.core.schema import SchemaMismatch, verify  # noqa: E402
from app.services.execution_service import ExecutorLoop  # noqa: E402

POLL_SEC = float(os.environ.get("ILCS_EXECUTOR_POLL_SEC", settings.advance_poll_sec))
# 只给模拟适配器补心跳。真实设备的在线状态必须由它自己上报，否则「在线」是我们编的。
# 正式环境默认关闭，显式打开会被 production_issues 拒绝启动。
SIMULATE_HEARTBEAT = settings.simulate_heartbeat


STANDBY_POLL_SEC = 5.0
MONITOR_ASSETS_SEC = 60 * 60


class _KeyValueFormatter(logging.Formatter):
    """一行一条、字段可检索的日志：时间、级别、事件与结构化字段。收集器不用再解析中文句子。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _logger() -> logging.Logger:
    logger = logging.getLogger("ilcs.executor")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_KeyValueFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("ILCS_LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger


log = _logger()


def info(event: str, **fields) -> None:
    log.info(event, extra={"fields": fields})


class _Stop:
    """收到 SIGTERM / SIGINT 后做完当前这一轮再退出：不在指令投递到一半时被杀。"""

    requested = False

    @classmethod
    def request(cls, signum, _frame) -> None:
        cls.requested = True
        info("收到退出信号，完成当前一轮后退出", signal=signal.Signals(signum).name)


def acquire_singleton():
    """同一时刻只允许一个执行器投递指令。

    指令领取、对账与轮询都按「只有我在处理这些指令」写的：两个副本同时对账，会把对方
    正在网络调用中的指令判成不一致。PostgreSQL 上用会话级 advisory lock 做主备——拿不到
    锁的副本待命，主副本退出或断线后自动接管。
    """
    announced = False
    while True:
        connection = engine.connect()
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:namespace, hashtext('ilcs-executor'))"),
            {"namespace": ADVISORY_NAMESPACE},
        ).scalar()
        connection.commit()
        if acquired:
            return connection
        connection.close()
        if not announced:
            info("另一个执行器正在运行，本进程待命")
            announced = True
        if _Stop.requested:
            return False
        time.sleep(STANDBY_POLL_SEC)


def singleton_alive(connection) -> bool:
    if connection is None:
        return True
    try:
        connection.execute(text("SELECT 1"))
        connection.commit()
        return True
    except Exception:
        return False


def main() -> int:
    signal.signal(signal.SIGTERM, _Stop.request)
    signal.signal(signal.SIGINT, _Stop.request)
    issues = settings.production_issues()
    if issues:
        log.error("执行器拒绝启动：配置门禁未通过", extra={"fields": {"issues": issues}})
        return 2
    try:
        revision = verify(engine)
    except SchemaMismatch as exc:
        log.error("执行器拒绝启动：库版本不兼容", extra={"fields": {"detail": str(exc)}})
        return 2
    singleton = acquire_singleton()
    if singleton is False:
        return 0
    info(
        "执行器已启动", revision=revision, poll_sec=POLL_SEC, simulate_heartbeat=SIMULATE_HEARTBEAT,
        pid=os.getpid(),
    )
    last_file_cleanup: float | None = None
    last_asset_monitor: float | None = None
    failures = 0
    while not _Stop.requested:
        if not singleton_alive(singleton):
            # 持锁连接断了，锁可能已被备用副本接管；退出由容器重启后重新竞争
            log.error("执行器互斥锁连接中断，退出以免与接管的副本同时投递")
            return 3
        try:
            monotonic_now = time.monotonic()
            cleanup_due = (
                last_file_cleanup is None
                or monotonic_now - last_file_cleanup >= max(60, settings.file_cleanup_interval_sec)
            )
            assets_due = (
                last_asset_monitor is None or monotonic_now - last_asset_monitor >= MONITOR_ASSETS_SEC
            )
            with SessionLocal() as db:
                report = ExecutorLoop(db).tick(
                    simulate_heartbeat=SIMULATE_HEARTBEAT, cleanup_files=cleanup_due,
                    monitor_assets=assets_due,
                )
            if cleanup_due:
                last_file_cleanup = monotonic_now
            if assets_due:
                last_asset_monitor = monotonic_now
            failures = 0
            counts = {key: value for key, value in report.items() if key != "at" and value}
            if counts:
                info("执行器一轮", **counts)
        except Exception:  # 执行器必须自愈：单次循环失败不退出进程
            failures += 1
            log.exception("执行器一轮失败", extra={"fields": {"consecutive_failures": failures}})
        # 连续失败时退避，避免数据库故障期间以轮询频率刷日志、压数据库
        delay = POLL_SEC if failures == 0 else min(60.0, POLL_SEC * (2 ** min(failures, 6)))
        deadline = time.monotonic() + delay
        while not _Stop.requested and time.monotonic() < deadline:
            time.sleep(min(0.5, delay))
    if singleton is not None:
        singleton.close()
    info("执行器已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
