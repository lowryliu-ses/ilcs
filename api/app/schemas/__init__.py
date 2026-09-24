"""接口输入模型。

约定：
- 修改与审核请求携带 `row_version`（预期对象版本），过期返回 409。
- 数量一律用字符串或整数写，不用浮点——账实差额不允许被二进制浮点吃掉。
- 事件类接口必带稳定的 `event_id`；它是业务级去重键，和请求头的幂等键不是一回事。
"""
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 数量：接受字符串或整数，拒绝浮点字面量
Quantity = Decimal | int | str


class Versioned(BaseModel):
    """带预期对象版本的修改请求。"""

    row_version: int | None = None


# ---------- 身份与签名 ----------

class LoginIn(BaseModel):
    username: str
    password: str
    organization_id: str | None = None


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class AccountCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    display_name: str
    # 主角色；`roles` 给出时以它为准（第一个是主角色），账号可同时挂多个角色
    role: Literal["researcher", "qa", "operator", "ehs", "admin"] | None = None
    roles: list[Literal["researcher", "qa", "operator", "ehs", "admin"]] | None = None
    default_lab_id: str = ""


class AccountPatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    role: Literal["researcher", "qa", "operator", "ehs", "admin"] | None = None
    roles: list[Literal["researcher", "qa", "operator", "ehs", "admin"]] | None = None
    account_state: Literal["active", "disabled"] | None = None
    membership_state: Literal["active", "revoked"] | None = None


class RolePermissionsIn(BaseModel):
    """组织的角色权限矩阵。系统管理员不在矩阵里；row_version 是读到的版本（从未改过为 0）。"""

    model_config = ConfigDict(extra="forbid")

    matrix: dict[str, list[str]]
    row_version: int = Field(ge=0)
    signature_id: str


class SignatureIn(BaseModel):
    password: str
    meaning: str
    action: str = ""
    target: str = ""
    note: str = ""
    object_version: int = 0


class Signed(BaseModel):
    """所有需要电子签名的写操作都带这个字段。"""

    signature_id: str


class ServiceIdentityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    name: str = ""
    # {"stations": ["ST-.."], "analysis_tasks": ["AT-.."] | "all", "instrument_serials": [..]}
    scopes: dict[str, Any] = {}


class ServiceIdentityPatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    scopes: dict[str, Any] | None = None


class ServiceIdentityStateIn(BaseModel):
    state: Literal["active", "disabled"]


# ---------- 人员与资质 ----------

class PersonCreateIn(BaseModel):
    code: str
    name: str
    lab_id: str = ""
    title: str = ""
    contact: str = ""
    employment_state: Literal["on_duty", "leave", "left"] = "on_duty"
    user_id: str = ""
    note: str = ""


class PersonPatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    lab_id: str | None = None
    title: str | None = None
    contact: str | None = None
    employment_state: Literal["on_duty", "leave", "left"] | None = None
    user_id: str | None = None
    note: str | None = None


class QualificationIn(BaseModel):
    scope_kind: Literal["capability", "sop", "safety"]
    scope_ref: str
    label: str = ""
    evidence_file_id: str = ""
    effective_from: datetime | None = None
    expires_at: datetime | None = None


class RevokeIn(BaseModel):
    reason: str


# ---------- 资产、校准与预约 ----------

class AssetCreateIn(BaseModel):
    asset_no: str
    name: str
    model: str = ""
    vendor: str = ""
    serial: str = ""
    firmware: str = ""
    lab_id: str = ""
    owner_person_id: str = ""
    location: str = ""
    state: Literal["active", "maintenance", "retired"] = "active"
    capacity: int = Field(default=1, ge=1)
    calibration_applicable: bool = True
    calibration_exempt_reason: str = ""
    note: str = ""


class AssetPatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    model: str | None = None
    vendor: str | None = None
    serial: str | None = None
    firmware: str | None = None
    lab_id: str | None = None
    owner_person_id: str | None = None
    location: str | None = None
    state: Literal["active", "maintenance", "retired"] | None = None
    capacity: int | None = Field(default=None, ge=1)
    calibration_applicable: bool | None = None
    calibration_exempt_reason: str | None = None
    note: str | None = None


class CalibrationIn(BaseModel):
    capability_scope: list[str] = []
    result: Literal["pass", "fail"] = "pass"
    effective_from: datetime | None = None
    expires_at: datetime | None = None
    certificate_file_id: str = ""
    note: str = ""


class StationLinkIn(BaseModel):
    station_id: str


class MaintenanceOrderIn(BaseModel):
    asset_id: str
    kind: Literal["preventive", "corrective", "inspection"] = "preventive"
    title: str = Field(min_length=1)
    detail: str = ""
    planned_start: datetime
    planned_end: datetime
    assignee_user_id: str = ""


class MaintenanceCompleteIn(Signed):
    result: Literal["pass", "fail"]
    record: str


class MaintenanceCancelIn(BaseModel):
    reason: str


class BookingIn(BaseModel):
    asset_id: str
    station_id: str = ""
    kind: Literal["maintenance", "manual", "calibration"] = "maintenance"
    starts_at: datetime
    ends_at: datetime
    reason: str = ""
    state: Literal["pending", "confirmed"] = "confirmed"


class BookingCancelIn(BaseModel):
    reason: str


# ---------- 方法（配方） ----------

class RecipeCreateIn(BaseModel):
    name: str
    plate: int = Field(ge=1, le=96)
    copy_from: str | None = None


class RecipePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    plate: int | None = Field(default=None, ge=1, le=96)
    risk: str | None = None
    design: str | None = None
    sop_version_id: str | None = None
    steps: list[dict[str, Any]] | None = None
    bom: list[dict[str, Any]] | None = None
    # 乐观并发：编辑器带上读到的版本，别人先改过就 409
    row_version: int | None = None


class MethodParamRule(BaseModel):
    default: float | None = None
    min: float | None = None
    max: float | None = None
    unit: str = ""


class MethodOutputRule(BaseModel):
    """数据输出规则：设备这一步应该回报哪些值、单位与合理范围。越界的值入库打标，不拒收。"""

    key: str
    label: str = ""
    unit: str = ""
    lo: float | None = None
    hi: float | None = None
    required: bool = False


class DeviceMethodIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    capability_id: str
    instrument_models: list[str] = []
    program: str = ""
    params: dict[str, MethodParamRule] = {}
    outputs: list[MethodOutputRule] = []
    dur_min: float = Field(default=0, ge=0)
    note: str = ""


class DeviceMethodPatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    capability_id: str | None = None
    instrument_models: list[str] | None = None
    program: str | None = None
    params: dict[str, MethodParamRule] | None = None
    outputs: list[MethodOutputRule] | None = None
    dur_min: float | None = Field(default=None, ge=0)
    note: str | None = None


class DataRuleIn(BaseModel):
    """前后逻辑校验：左指标 op 右指标 × factor + offset，或左指标 op 常数。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    left_metric: str
    op: Literal["<", "<=", ">", ">=", "==", "!="] = "<="
    right_metric: str = ""
    right_value: float | None = None
    factor: float = 1.0
    offset: float = 0.0
    severity: Literal["flag", "reject"] = "flag"
    enabled: bool = True
    note: str = ""


class DataRulePatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    left_metric: str | None = None
    op: Literal["<", "<=", ">", ">=", "==", "!="] | None = None
    right_metric: str | None = None
    right_value: float | None = None
    factor: float | None = None
    offset: float | None = None
    severity: Literal["flag", "reject"] | None = None
    enabled: bool | None = None
    note: str | None = None


class SimulateIn(BaseModel):
    """执行前仿真：并发几个批次、从什么时候开始、是否叠加当前时间线。"""

    concurrency: int = Field(default=1, ge=1, le=20)
    start_from: datetime | None = None
    use_timeline: bool = True


class RecipeTransitionIn(Signed):
    target_state: Literal["approved", "released", "retired"]


class GoldenBatchIn(Signed):
    batch_id: str


# ---------- 实验方案 ----------

class PlanCreateIn(BaseModel):
    name: str
    recipe_id: str
    plan_type: Literal["matrix", "single_condition", "commissioned_test"] = "matrix"
    project_id: str = ""
    goal: str = ""
    repeats: int = Field(default=1, ge=1, le=12)
    layout: Literal["sequential", "randomized"] = "sequential"
    seed: int = 1
    factors: list[dict[str, Any]] = []
    control: dict[str, Any] | None = None
    design_space: dict[str, Any] = {}
    design_points: list[list[Any]] = []
    sample_count: int = Field(default=0, ge=0, le=96)
    sample_ids: list[str] = []
    required_metrics: list[str] = []
    resource_requirements: list[dict[str, Any]] = []


class PlanPatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    plan_type: Literal["matrix", "single_condition", "commissioned_test"] | None = None
    project_id: str | None = None
    goal: str | None = None
    repeats: int | None = Field(default=None, ge=1, le=12)
    layout: Literal["sequential", "randomized"] | None = None
    seed: int | None = None
    factors: list[dict[str, Any]] | None = None
    control: dict[str, Any] | None = None
    # 设计空间随方案审批冻结，约束之后外部优化器的提案
    design_space: dict[str, Any] | None = None
    design_points: list[list[Any]] | None = None
    sample_count: int | None = Field(default=None, ge=0, le=96)
    sample_ids: list[str] | None = None
    required_metrics: list[str] | None = None
    resource_requirements: list[dict[str, Any]] | None = None


class ProposalIn(BaseModel):
    """下一轮实验提案。proposal_id 是提案方的稳定编号：同号重发回放原结论。"""

    proposal_id: str = Field(min_length=1, max_length=128)
    source: str = Field(default="", max_length=128)
    model_version: str = Field(default="", max_length=128)
    rationale: str = ""
    # 每个点是 {因子名: 水平}；因子名必须与方案一致
    points: list[dict[str, Any]] = Field(min_length=1, max_length=96)
    repeats: int | None = Field(default=None, ge=1, le=12)


class DecisionIn(BaseModel):
    """审批 / 审核的统一结论输入。不接受任意目标状态。"""

    conclusion: Literal["approved", "rejected"]
    reason: str = ""
    signature_id: str | None = None
    effective_from: datetime | None = None


# ---------- 实验任务 ----------

class TaskCreateIn(BaseModel):
    plan_id: str
    title: str = ""
    owner_user_id: str = ""
    reviewer_user_id: str = ""
    sample_ids: list[str] = []
    due_at: datetime | None = None
    priority: int = Field(default=2, ge=1, le=5)
    note: str = ""
    # 任务树与依赖：挂在哪个父任务下、依赖哪些上游任务
    parent_id: str = ""
    depends_on: list[str] = []


class TaskDecomposeIn(BaseModel):
    """拆分任务：按样本每份 chunk_size 个（缺省按方法样品位），或拆成 parts 份；sequential 时后一份依赖前一份。"""

    chunk_size: int | None = Field(default=None, ge=1, le=96)
    parts: int | None = Field(default=None, ge=2, le=50)
    sequential: bool = False


class TaskDependenciesIn(BaseModel):
    depends_on: list[str] = []


class TaskAssignIn(Versioned):
    assignee_user_id: str
    reason: str = ""


class CancelIn(BaseModel):
    reason: str


# ---------- 批次与排程 ----------

class BatchCreateIn(BaseModel):
    plan_id: str
    task_id: str = ""
    priority: int = Field(default=2, ge=1, le=5)
    note: str = ""


class ScheduleIn(BaseModel):
    start_from: datetime | None = None
    prefer_station_id: str | None = None


class RescheduleIn(BaseModel):
    from_step: int = Field(ge=0)
    start_from: datetime


class DispatchIn(Signed):
    manual_review: bool = False
    reason: str = ""


class HoldIn(BaseModel):
    reason: str = ""


class RecoverIn(Signed):
    strategy: Literal["resume", "retry", "abort"]
    verified: bool = False


class AbortIn(Signed):
    reason: str = ""


class OptimizeIn(BaseModel):
    batch_ids: list[str]
    start_from: datetime | None = None
    # optimize（先按交付期拖期、再按总跨度搜索顺序）/ priority / deadline / fifo；缺省取部署配置
    mode: Literal["optimize", "priority", "deadline", "fifo"] | None = None


class ProposalRequestIn(BaseModel):
    """人工请求重排建议：选中的批次，或某台工位（标为不用）上的全部批次。"""

    batch_ids: list[str] = []
    station_id: str = ""
    reason: str = ""


class ProposalDismissIn(BaseModel):
    note: str = ""


class ApplyOptimizedIn(BaseModel):
    order: list[str]
    start_from: datetime | None = None


# ---------- 步骤运行 ----------

class StepSubmitIn(Versioned):
    """人工步骤提交。缺项不推进，所以字段值与核对勾选都要给全。"""

    form_data: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    note: str = ""
    signature_id: str | None = None


class GateDecisionIn(Signed):
    """保持中的质检关卡人工判定：放行或判不合格，都要写依据。"""

    conclusion: Literal["approved", "rejected"]
    reason: str


class BranchDecisionIn(Versioned):
    """人工选择分支出口。判据缺失而保持的分支必须带签名。"""

    case: str
    reason: str
    signature_id: str | None = None


class SkipStepIn(Signed):
    step_id: str
    reason: str


class RerunFromIn(Signed):
    step_id: str
    reason: str


class BatchSignalIn(BaseModel):
    """批次业务事件。`event_id` 是发送方的去重键：同一事件重发不会唤醒两个等待节点。"""

    name: str = Field(min_length=1, max_length=64)
    event_id: str = ""
    payload: dict[str, Any] = {}


class ExceptionHandleIn(BaseModel):
    action: Literal["claim", "resolve", "close"]
    note: str = ""


class ExceptionRuleIn(BaseModel):
    """异常策略：某类异常用什么动作处理。match 可按 capability / station_id / step_kind / recipe_id / step_id 细分。"""

    name: str
    category: str
    action: Literal["retry", "reroute", "skip", "reschedule", "hold"]
    match: dict[str, Any] = {}
    params: dict[str, Any] = {}
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True
    note: str = ""
    row_version: int | None = None


class WebhookIn(BaseModel):
    name: str
    url: str
    topics: list[str]
    enabled: bool = True


class WebhookPatchIn(BaseModel):
    name: str | None = None
    url: str | None = None
    topics: list[str] | None = None
    enabled: bool | None = None
    row_version: int | None = None


class StepReviewIn(Versioned):
    conclusion: Literal["approved", "rejected"]
    reason: str = ""
    signature_id: str


# ---------- 样本 ----------

class SampleCreateIn(BaseModel):
    id: str = ""
    barcode: str = ""
    project_id: str = ""
    source: str = ""
    sample_type: str = ""
    parent_id: str | None = None
    quantity: Quantity | None = None
    unit: str = ""
    storage_condition: str = ""
    current_location: str = ""
    custodian: str = ""
    lifecycle_state: Literal[
        "registered", "received", "in_use", "stored", "exhausted", "disposed"
    ] = "registered"
    note: str = ""


class SampleReceiveIn(BaseModel):
    # 扫码重复提交靠这个键去重，不多写交接
    event_key: str
    to_location: str = ""
    from_party: str = ""
    to_party: str = ""
    quantity: Quantity | None = None
    unit: str = ""
    confirm_method: Literal["barcode", "manual", "signature"] = "barcode"
    note: str = ""
    occurred_at: datetime | None = None


class SampleTransferIn(BaseModel):
    event_key: str
    kind: Literal["handover", "store", "dispose", "move"] = "handover"
    from_location: str = ""
    to_location: str = ""
    # 去向是登记过的库位 / 放置位时填编号：样本的结构化位置随之更新
    to_location_id: str = ""
    from_party: str = ""
    to_party: str = ""
    quantity: Quantity | None = None
    unit: str = ""
    confirm_method: Literal["barcode", "manual", "signature"] = "barcode"
    note: str = ""
    occurred_at: datetime | None = None


class SplitChildIn(BaseModel):
    id: str = ""
    barcode: str = ""
    quantity: Quantity
    sample_type: str = ""
    storage_condition: str = ""
    current_location: str = ""
    note: str = ""


class SampleSplitIn(BaseModel):
    event_key: str = ""
    children: list[SplitChildIn]
    loss: Quantity = "0"
    loss_reason: str = ""


# ---------- 物料与库存 ----------

class MaterialCreateIn(BaseModel):
    code: str
    name: str
    base_unit: str
    category: str = ""
    cas: str = ""
    conversions: dict[str, Quantity] = {}
    external_ref: str = ""
    ghs: list[str] = []


class LotCreateIn(BaseModel):
    id: str
    material: str
    material_id: str = ""
    cas: str = ""
    type: str = ""
    qty: Quantity
    unit: str
    expiry: str
    open_expiry: str = ""
    open_expiry_basis: str = ""
    storage: str = ""
    sds: str = ""
    compat: str = ""
    ghs: list[str] = []
    event_id: str = ""


class LotPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # qty 收在这里只是为了给出可读的拒绝理由：数量变化必须走库存事件或盘点调整
    qty: Quantity | None = None
    material: str | None = None
    cas: str | None = None
    type: str | None = None
    unit: str | None = None
    expiry: str | None = None
    storage: str | None = None
    sds: str | None = None
    compat: str | None = None
    opened: str | None = None
    open_expiry: str | None = None
    open_expiry_basis: str | None = None
    ghs: list[str] | None = None


class LotOpenIn(BaseModel):
    opened: str = ""
    open_expiry: str = ""
    # 录了开封截止时间就必须写依据，不按类别编造天数
    open_expiry_basis: str = ""


class LotScrapIn(Signed):
    reason: str


class LotAdjustIn(BaseModel):
    qty: Quantity
    reason: str


class InventoryLineIn(BaseModel):
    line_no: int | None = None
    lot_id: str
    reservation_id: int | None = None
    quantity: Quantity
    unit: str = ""
    note: str = ""


class InventoryEventIn(BaseModel):
    """库存业务事件。event_id 是业务级去重键，重试必须复用同一个值。"""

    event_id: str
    event_type: Literal[
        "receive", "reserve", "issue", "consume", "loss", "return", "release", "adjust"
    ]
    source: Literal["manual", "weighing", "return", "device"] = "manual"
    batch_id: str = ""
    step_run_id: str = ""
    command_id: str = ""
    reason: str = ""
    items: list[InventoryLineIn]


class WasteCreateIn(BaseModel):
    id: str
    kind: str
    capacity_l: float = Field(default=20, gt=0)
    level_pct: float = Field(default=0, ge=0, le=100)


class WastePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str | None = None
    capacity_l: float | None = Field(default=None, gt=0)
    level_pct: float | None = Field(default=None, ge=0, le=100)


# ---------- 指标与检测 ----------

class MetricCreateIn(BaseModel):
    code: str
    name: str
    version: str = "v1"
    value_type: Literal["number", "text", "enum"] = "number"
    unit: str = ""
    method_version: str = ""
    sample_types: list[str] = []
    rules: dict[str, Any] = {}


class MetricPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    value_type: Literal["number", "text", "enum"] | None = None
    unit: str | None = None
    method_version: str | None = None
    sample_types: list[str] | None = None
    rules: dict[str, Any] | None = None


class AnalysisTaskCreateIn(BaseModel):
    sample_id: str = ""
    physical_sample_id: str = ""
    method: str = ""
    method_version: str = ""
    required_metrics: list[str]
    round_no: int | None = None
    external_ref: str = ""
    retest_of: str = ""


class RetestIn(BaseModel):
    reason: str = ""
    method: str = ""
    method_version: str = ""
    required_metrics: list[str] = []


class MetricValueIn(BaseModel):
    metric_version_id: str
    value: Any | None = None
    unit: str = ""
    # 声明无法测得时写原因；缺值不当成 0
    not_measured_reason: str = ""


class ResultIngestIn(BaseModel):
    """新回传契约。来源与组织由认证确定，不由请求体指定。"""

    event_id: str
    task_id: str
    sample_id: str = ""
    collected_at: datetime | None = None
    parser_version: str = ""
    raw_file_id: str = ""
    instrument_serial: str = ""
    # 测出这些值的工位（可选）；服务身份须在 stations 授权范围内
    station_id: str = ""
    metrics: list[MetricValueIn]


class ManualResultIn(BaseModel):
    event_id: str
    sample_id: str = ""
    collected_at: datetime | None = None
    parser_version: str = ""
    raw_file_id: str = ""
    station_id: str = ""
    instrument_serial: str = ""
    metrics: list[MetricValueIn]


class ResultReviewIn(BaseModel):
    conclusion: Literal["approved", "rejected"]
    # 审核通过必须同时给质量判定：审核完成不等于质量有效
    quality: Literal["unassessed", "valid", "suspect", "invalid"] = "unassessed"
    reason: str = ""
    result_version: int | None = None
    signature_id: str


class ResultRevisionIn(BaseModel):
    value: Any | None = None
    unit: str = ""
    not_measured_reason: str = ""
    collected_at: datetime | None = None
    raw_file_id: str = ""
    parser_version: str = ""
    reason: str


class FlagIn(BaseModel):
    quality: Literal["valid", "suspect", "invalid"]
    note: str = ""


# ---------- SOP ----------

class SopVersionCreateIn(BaseModel):
    code: str
    title: str
    version: str = ""
    file_id: str = ""
    capability_scope: list[str] = []
    sample_types: list[str] = []
    requires_training_ack: bool = False
    effective_from: datetime | None = None


class SopVersionPatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    file_id: str | None = None
    capability_scope: list[str] | None = None
    sample_types: list[str] | None = None
    requires_training_ack: bool | None = None
    effective_from: datetime | None = None


class RetireIn(BaseModel):
    retired: bool = True
    reason: str = ""


# ---------- 报告 ----------

class ReportCreateIn(BaseModel):
    batch_id: str = ""
    task_id: str = ""
    plan_id: str = ""
    title: str = ""
    conclusion: str = ""


class ReportPatchIn(Versioned):
    conclusion: str | None = None
    refresh: bool = False


class PublishIn(Signed):
    pass


# ---------- 资源 ----------

class LimitsIn(Signed):
    limits: dict[str, dict[str, list[float]]]
    row_version: int | None = None


class CapabilityIn(Signed):
    id: str
    name: str
    params: dict[str, str]
    recovery: dict[str, Any] = {}
    stations: list[str] = []


class ManualReviewIn(BaseModel):
    note: str = ""


class CommandVerifyIn(Signed):
    """结果未知指令的现场核查结论。结论三选一，依据必填，签名针对这条指令。"""

    conclusion: Literal["executed", "not_executed", "partial"]
    note: str
    # 已执行时现场核实的实际交付量，写进人工核实来源的检查点
    delivered: dict[str, Any] = {}


class StationCreateIn(Signed):
    id: str
    name: str
    island: int = 0
    model: str = ""
    cal_due: str = ""
    positions: int = Field(default=1, ge=1)
    channels: int = Field(default=1, ge=1, le=512)
    limits: dict[str, dict[str, list[float]]] = {}
    asset_id: str = ""
    protocol: str = ""
    adapter_driver: str = "simulation"
    adapter_version: str = ""
    adapter_config: dict[str, Any] = {}
    credential_ref: str = ""
    adapter_kind: Literal["simulation", "real"] = "simulation"
    supports_hold: bool = True
    supports_abort: bool = True
    supports_query: bool = True
    supports_dedup: bool = True


class AdapterPatchIn(Signed):
    model_config = ConfigDict(extra="forbid")

    protocol: str | None = None
    driver: str | None = None
    version: str | None = None
    kind: Literal["simulation", "real"] | None = None
    config: dict[str, Any] | None = None
    credential_ref: str | None = None
    enabled: bool | None = None
    supports_hold: bool | None = None
    supports_abort: bool | None = None
    supports_query: bool | None = None
    supports_dedup: bool | None = None
    note: str | None = None
    row_version: int


class StationPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    model: str | None = None
    island: int | None = None
    positions: int | None = Field(default=None, ge=1)
    channels: int | None = Field(default=None, ge=1, le=512)
    cal_due: str | None = None
    asset_id: str | None = None
    # 乐观并发：带上读到的版本，别人先改过就 409，不做后写覆盖前写
    row_version: int | None = None


class CapabilityPatchIn(Signed):
    name: str | None = None
    params: dict[str, str] | None = None
    recovery: dict[str, Any] | None = None


class ReadinessIn(BaseModel):
    clean: bool = True
    status: Literal["idle", "running", "fault", "offline"] = "idle"
    row_version: int | None = None


class HeartbeatIn(BaseModel):
    connected: bool = True
    site_interlock: bool = False
    accepts_commands: bool = True
    # 业务数据，用于与资产档案核对；不能代替来源认证
    instrument_serial: str = ""


class TelemetryPointIn(BaseModel):
    metric: str = Field(min_length=1, max_length=64)
    value: float
    setpoint: float | None = None
    device_ts: datetime
    quality: Literal["good", "bad", "uncertain"] = "good"
    # 可选：设备按孔位或样本报的点，归到本批次对应样本；整板遥测不填
    well: str = Field(default="", max_length=16)
    sample_id: str = Field(default="", max_length=64)


class TelemetryIn(BaseModel):
    """设备遥测上报。同一 event_id 重发只入库一次；command_id 可选，用于归到批次。"""

    event_id: str = Field(min_length=1, max_length=128)
    command_id: str = ""
    points: list[TelemetryPointIn] = Field(min_length=1)


class CommandEventIn(BaseModel):
    """设备回执。必须绑定原 command_id（在路径上），并给出明确结论。"""

    outcome: Literal["accepted", "done", "failed"]
    device_ts: datetime | None = None
    quality: str = "good"
    delivered: dict[str, Any] = {}
    error: str = ""


# ---------- 报警 ----------

class AlarmClearIn(Signed):
    reason: str


class ShelveIn(BaseModel):
    until: str


# ---------- 文件 ----------

class FileAttachIn(BaseModel):
    ref_type: str
    ref_id: str


# ---------- 载具与位置 ----------

class LabwareCreateIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=80)
    type_id: str
    location_id: str | None = None
    note: str = ""


class LabwareMoveIn(BaseModel):
    """扫码放置。`to_location_id` 为空表示从产线取下；条码必须与载具一致。"""

    barcode: str
    to_location_id: str | None = None
    reason: str = ""


class LabwareBindIn(BaseModel):
    labware_id: str


class LocationCreateIn(BaseModel):
    id: str = Field(min_length=1, max_length=60)
    name: str = ""
    kind: Literal["nest", "hotel", "buffer", "storage"] = "nest"
    station_id: str = ""
    group: str = ""
    position: int = 0
    accepts: list[str] = Field(default_factory=list)


class LocationActiveIn(BaseModel):
    active: bool
