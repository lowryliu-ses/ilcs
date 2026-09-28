-- ILCS 中间库契约（sql_table_v1）：ILCS 往作业表写作业，设备侧软件（厂家调度软件、集成商服务）轮询作业表、
-- 执行后回写状态与结果，并在设备表里维持心跳。表名可在适配器配置里改（jobs_table / device_table），列名不能改。
-- 下面是 PostgreSQL 写法；SQL Server 把 TEXT 换成 NVARCHAR(MAX)、VARCHAR 换成 NVARCHAR，MySQL 用 utf8mb4。
--
-- 约定：
-- * command_id 是主键：ILCS 重投同一指令号只会撞主键，设备侧据此去重（不要再按别的列去重）。
-- * state 由 ILCS 写 'new'；之后只由设备侧改：accepted / running / held / done / failed / rejected / aborted。
--   rejected = 设备明确没动作（参数非法、联锁、忙）；failed = 动作了但失败；error 写原因。
-- * 保持 / 终止也是一行作业：task_type = 'hold' / 'abort'，target_command_id 指向要停的作业；设备处理后把这一行改成
--   done（或 rejected）。
-- * delivered_json：{"materials": [...], "<实测参数>": 值, ...}；telemetry_json：[{"metric", "value", "setpoint"}]。
-- * 时间一律 ISO-8601 字符串（带时区偏移，或 UTC 加 Z）。
-- * 设备表的 heartbeat_at 由设备侧周期更新；超过 heartbeat_stale_sec 不变 ILCS 判失联。
--   simulator = 1 的设备（模拟器）正式环境拒绝接入。
-- * ILCS 用的数据库账号只需要：作业表 SELECT / INSERT，设备表 SELECT。

CREATE TABLE ilcs_jobs (
    command_id        VARCHAR(64)  PRIMARY KEY,
    station_id        VARCHAR(64)  NOT NULL,
    capability        VARCHAR(64)  NOT NULL,
    task_type         VARCHAR(16)  NOT NULL,          -- dispatch | resume | retry | transfer | hold | abort
    target_command_id VARCHAR(64),
    program           VARCHAR(128),                   -- 设备方法的设备端程序
    params_json       TEXT         NOT NULL,          -- 指令参数（含孔位矩阵 wells）
    context_json      TEXT         NOT NULL,          -- {"batch_id", "step_id", "step_index", "method"}
    state             VARCHAR(16)  NOT NULL,
    quality           VARCHAR(16),                    -- good | bad | uncertain
    delivered_json    TEXT,
    telemetry_json    TEXT,
    error             TEXT,
    device_ts         VARCHAR(40),
    created_at        VARCHAR(40)  NOT NULL,
    updated_at        VARCHAR(40)  NOT NULL
);
CREATE INDEX ilcs_jobs_state ON ilcs_jobs (state, created_at);

CREATE TABLE ilcs_device (
    device_id         VARCHAR(64)  PRIMARY KEY,
    model             VARCHAR(128),
    vendor            VARCHAR(128),
    firmware          VARCHAR(64),
    heartbeat_at      VARCHAR(40),
    interlock         INTEGER      DEFAULT 0,
    accepts_commands  INTEGER      DEFAULT 1,
    simulator         INTEGER      DEFAULT 0,
    methods_json      TEXT                             -- [{"program", "name", "capability"}]：设备端程序目录
);
