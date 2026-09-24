from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

API_DIR = Path(__file__).resolve().parents[2]
# 只支持 PostgreSQL。开发默认连 `scripts/dev-db.sh` 拉起的本机容器，正式环境由 .env 指定。
# 不再保留 SQLite：行锁、SKIP LOCKED、advisory lock、部分唯一索引与精确小数在两种库上
# 行为不同，一份代码两套语义，开发期验证的永远不是正式环境跑的那条路。
DEFAULT_DATABASE_URL = "postgresql+psycopg2://ilcs:ilcs-dev@127.0.0.1:55432/ilcs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ILCS_")
    app_name: str = "ILCS 实验室平台 API"
    environment: Literal["development", "test", "production"] = "development"
    # 仅测试 / 演示：允许系统管理员审批、复核自己提交或录入的内容，每次放行都留审计。
    # 正式环境开启即判配置不合格，服务不进入健康状态
    admin_self_approval: bool = False
    secret_key: str = "ilcs-dev-change-me"
    # 口令摘要的 pepper 与 JWT 密钥分离；轮换令牌密钥不应使全部账号口令失效。
    # 为空时仅为兼容已有开发库而回落到 secret_key。
    password_pepper: str = ""
    access_ttl_min: int = 12 * 60
    sign_ttl_sec: int = 120
    database_url: str = DEFAULT_DATABASE_URL
    # PostgreSQL 连接池
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_sec: int = 30
    db_pool_recycle_sec: int = 1800
    cors_origins: str = "http://localhost:5174,http://127.0.0.1:5174"

    transfer_min: int = 10
    clean_min: int = 10
    heartbeat_degraded_sec: int = 5
    heartbeat_stale_sec: int = 300
    schedule_expiry_min: int = 30
    # 按时开工：设备动作最多比排程时间窗提前这么多分钟投递
    early_start_tolerance_min: float = 15
    # 实际开工晚于计划超过这个分钟数，本批下游时间窗整体顺延
    realign_grace_min: float = 1
    telemetry_points_per_step: int = 12
    # 设备遥测保留期；执行器每小时清理更早的点
    telemetry_retention_days: int = 365
    telemetry_max_points_per_request: int = 1000

    # ---------- 文件 ----------
    # 受控目录，放持久卷；不要指向前端静态目录
    file_root: str = "./data/files"
    file_max_bytes: int = 20 * 1024 * 1024
    # 上传后未被 SOP/资质/校准/结果/报告引用的文件只保留一个处置窗口，随后由执行器清理。
    file_orphan_retention_hours: int = 24
    file_cleanup_interval_sec: int = 60 * 60
    file_cleanup_batch_size: int = 100
    # 反向代理的 client_max_body_size 必须与这里同步，否则大文件会在网关静默失败
    file_allowed_types: str = (
        "application/pdf,text/csv,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "image/png,image/jpeg"
    )

    # ---------- 真实设备网关 ----------
    # 防止适配器配置把带凭据的请求发往任意内网地址。正式环境必须显式列出设备网关。
    adapter_allowed_hosts: str = "127.0.0.1,localhost"
    adapter_credential_root: str = "/run/secrets/ilcs"

    # 只给模拟适配器补心跳。未设置时开发 / 测试默认开、正式环境默认关；
    # 正式环境显式打开会被硬门禁拒绝——那里的「在线」必须由设备自己上报。
    executor_simulate_heartbeat: bool | None = None

    # ---------- 后台推进器 ----------
    advance_poll_sec: float = 5.0
    advance_batch_size: int = 20
    # 领取超时：崩溃进程占着的 processing 行过了这个时间会被重新领取
    advance_claim_timeout_sec: int = 120
    # 推进事件遇到非业务性错误（数据库抖动、连接中断）按指数退避重试；
    # 超过次数才判为失败并对批次报警，不让一次瞬时错误把流程永久卡住。
    advance_max_attempts: int = 6
    advance_retry_base_sec: int = 5
    advance_retry_max_sec: int = 300

    # ---------- 资质与到期提醒 ----------
    qualification_warn_days: int = 30
    lot_expiry_warn_days: int = 30
    calibration_warn_days: int = 30

    # ---------- 多批次优化 ----------
    # auto：装了 ortools 就先用 CP-SAT 给一个候选顺序，再与内置顺序搜索的结果比；search：只用内置搜索
    scheduler_backend: str = "auto"
    scheduler_search_budget_sec: float = 3.0
    scheduler_cpsat_time_limit_sec: float = 5.0
    scheduler_max_batches: int = 40
    # 排程模式缺省值：optimize（先按交付期拖期、再按总跨度与加权完成时间搜索顺序）/ priority / deadline / fifo
    scheduler_mode: str = "optimize"
    # 事件触发的重排建议是否自动应用。只对还没下发的批次生效；在途批次的建议一律待调度确认
    auto_reschedule: bool = False

    # ---------- 载具与转运 ----------
    # 下发前必须绑定载具（全自动产线打开；半自动 / 人工上下料的部署保持关闭）
    labware_required: bool = False

    # ---------- 并发执行器与变更推送 ----------
    # 执行器按工位并发处理设备 I/O 的线程数；同一工位同一时刻只有一个线程
    executor_workers: int = 8
    # 每轮等工位线程的最长时间；没做完的留在后台继续做，该工位本轮不再派活
    executor_station_wait_sec: float = 2.0
    # 工位线程超过这个时长没返回就记一次告警日志（设备网关可能卡住）
    executor_station_stuck_sec: float = 60.0
    # 两轮之间的最小间隔：通知风暴时不空转
    executor_min_gap_sec: float = 0.2
    # 推送连接的最长存活时间：到点由服务端关闭、客户端重连并重新鉴权（令牌可能已撤销）
    stream_max_sec: float = 300.0
    stream_keepalive_sec: float = 15.0

    # ---------- 执行器存活与指令超时 ----------
    # 执行器超过这么久没有写存活记录，执行门关闭；0 表示不检查（仅限单元测试）
    executor_stale_sec: int = 60
    # 设备指令按步骤预计时长的倍数判超时：先报警，超过硬上限转结果未知
    command_overdue_factor: float = 1.5
    command_hard_limit_factor: float = 3.0
    command_timeout_grace_min: float = 5.0
    # 保持 / 终止这类安全指令没有步骤时长可参照，用固定上限
    control_command_timeout_min: float = 10.0

    # ---------- 设备回传的物料消耗 ----------
    # 设备回报的实际消耗与计划量偏差超过这个百分比时报警并标记待复核（仍按实际量入账）
    consumption_deviation_pct: float = 5.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_media_types(self) -> set[str]:
        return {t.strip() for t in self.file_allowed_types.split(",") if t.strip()}

    @field_validator("database_url")
    @classmethod
    def _postgresql_only(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+")):
            raise ValueError("ILCS_DATABASE_URL 必须是 PostgreSQL 连接串（postgresql+psycopg2://...）；不再支持 SQLite")
        return value

    @property
    def simulate_heartbeat(self) -> bool:
        if self.executor_simulate_heartbeat is None:
            return self.environment != "production"
        return self.executor_simulate_heartbeat

    @property
    def simulation_allowed(self) -> bool:
        """模拟适配器只在开发与测试环境可用。正式环境没有例外开关。"""
        return self.environment != "production"

    @property
    def adapter_allowed_host_set(self) -> set[str]:
        return {host.strip().lower() for host in self.adapter_allowed_hosts.split(",") if host.strip()}

    def production_issues(self) -> list[str]:
        """正式环境硬门禁。示例占位符、弱密钥或通配配置不能进入健康状态。"""
        if self.environment != "production":
            return []
        issues: list[str] = []
        if len(self.secret_key) < 32 or self.secret_key in {"ilcs-dev-change-me", "__CHANGE_ME__"}:
            issues.append("ILCS_SECRET_KEY 必须设置为至少 32 位的随机值")
        if len(self.password_pepper) < 32 or self.password_pepper.startswith("__CHANGE_ME"):
            issues.append("ILCS_PASSWORD_PEPPER 必须设置为至少 32 位且独立于令牌密钥的随机值")
        if self.file_orphan_retention_hours < 1:
            issues.append("ILCS_FILE_ORPHAN_RETENTION_HOURS 必须至少为 1")
        if self.file_cleanup_interval_sec < 60:
            issues.append("ILCS_FILE_CLEANUP_INTERVAL_SEC 必须至少为 60")
        if self.file_cleanup_batch_size < 1:
            issues.append("ILCS_FILE_CLEANUP_BATCH_SIZE 必须至少为 1")
        if "*" in self.cors_origin_list:
            issues.append("production 模式不允许 CORS 通配来源")
        if (
            not self.adapter_allowed_host_set
            or "*" in self.adapter_allowed_host_set
            or self.adapter_allowed_host_set <= {"127.0.0.1", "localhost", "::1"}
        ):
            issues.append("ILCS_ADAPTER_ALLOWED_HOSTS 必须显式列出允许连接的设备网关主机")
        if self.admin_self_approval:
            issues.append("production 模式不允许 ILCS_ADMIN_SELF_APPROVAL=1；职责分离必须对所有人强制")
        if self.executor_simulate_heartbeat:
            issues.append("production 模式不允许 ILCS_EXECUTOR_SIMULATE_HEARTBEAT=1；设备在线状态必须由设备上报")
        if self.executor_stale_sec <= 0:
            issues.append("ILCS_EXECUTOR_STALE_SEC 必须大于 0：正式环境必须检查执行器存活")
        if self.advance_max_attempts < 1:
            issues.append("ILCS_ADVANCE_MAX_ATTEMPTS 必须至少为 1")
        return issues


settings = Settings()
