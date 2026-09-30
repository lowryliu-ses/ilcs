"""接口输入模型。

约定：
- 修改与审核请求携带 `row_version`（预期对象版本），过期返回 409。
- 数量一律用字符串或整数写，不用浮点——账实差额不允许被二进制浮点吃掉。
- 事件类接口必带稳定的 `event_id`；它是业务级去重键，和请求头的幂等键不是一回事。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ..core.clock import as_utc

# 数量：接受字符串或整数，拒绝浮点字面量
Quantity = Decimal | int | str

# 时间：库里一律存无时区 UTC。前端发的是带偏移的 ISO（toISOString 带 Z），在入口按偏移换算、去掉时区，
# 不带偏移的视为 UTC（与 core.clock.as_utc 同一口径）。不在入口换算的话，服务里拿它和 now() 一比较
# （到期判断、区间重叠）就是 TypeError，接口直接 500
UtcDatetime = Annotated[datetime, AfterValidator(as_utc)]


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
    effective_from: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None


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
    effective_from: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    certificate_file_id: str = ""
    note: str = ""


class StationLinkIn(BaseModel):
    station_id: str
    # 工位已关联别的资产时要明确说「移过来」：容量、校准许可与型号都改按新资产计
    move: bool = False


class MaintenanceOrderIn(BaseModel):
    asset_id: str
    kind: Literal["preventive", "corrective", "inspection"] = "preventive"
    title: str = Field(min_length=1)
    detail: str = ""
    planned_start: UtcDatetime
    planned_end: UtcDatetime
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
    starts_at: UtcDatetime
    ends_at: UtcDatetime
    reason: str = ""
    state: Literal["pending", "confirmed"] = "confirmed"


class BookingCancelIn(BaseModel):
    reason: str


# ---------- 方法（流程） ----------

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
    # 关联的检测指标：设备回报这个值时按样本写成该指标的检测结果（进数据审核）；不填只进检查点
    metric_id: str = ""


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


class ApprovalLevelIn(BaseModel):
    label: str = ""
    # 指定审批人（账号 ID）；不指定则任何有审批权限的人都可以审这一级
    assignee_id: str = ""


class PlanSubmitIn(BaseModel):
    """提交方案评审。approvers 为空就是一级「QA 审批」；最多 5 级，逐级审。"""

    approvers: list[ApprovalLevelIn] = Field(default_factory=list, max_length=5)


class PlanRestoreIn(Versioned):
    from_version: int = Field(ge=1)


class PlanTemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    # 从已有方案取结构；不给就用下面的字段
    from_plan_id: str = ""
    plan_type: Literal["matrix", "single_condition", "commissioned_test"] = "matrix"
    recipe_id: str = ""
    goal: str | None = None
    factors: list[dict[str, Any]] | None = None
    control: dict[str, Any] | None = None
    repeats: int | None = Field(default=None, ge=1, le=12)
    layout: Literal["sequential", "randomized"] | None = None
    seed: int | None = None
    design_space: dict[str, Any] | None = None
    sample_count: int | None = Field(default=None, ge=0, le=96)
    required_metrics: list[str] | None = None
    resource_requirements: list[dict[str, Any]] | None = None


class CommentIn(BaseModel):
    target_type: Literal["plan", "sop_version", "recipe", "report_version"]
    target_id: str
    anchor: str = Field(default="", max_length=64)
    body: str = Field(min_length=1, max_length=4000)


class SopStepIn(BaseModel):
    # 稳定标识：流程节点按它引用这一步。编辑时原样带回；新加的步骤留空，由服务端生成
    key: str = Field(default="", max_length=32)
    title: str
    kind: Literal["device", "manual", "wait", "review"] = "manual"
    capability: str = ""
    params: dict[str, float] = {}
    duration_min: float = Field(default=0, ge=0)
    instructions: str = ""
    checks: list[str] = []


class SopStepsIn(Versioned):
    steps: list[SopStepIn] = Field(max_length=200)


class SopRestoreIn(BaseModel):
    """从历史版本恢复：产生一个新的草稿版本，内容（附件、适用范围、结构化步骤）取自历史版本。"""

    version: str = ""


class SopRecipeIn(BaseModel):
    name: str = ""
    plate: int = Field(default=8, ge=1, le=96)


class EnvironmentReadingIn(BaseModel):
    zone: str = Field(min_length=1, max_length=64)
    metric: str = Field(min_length=1, max_length=32)
    # NaN / 无穷不收：它们和任何上下限比较都是假，会让越界的环境悄悄「通过」
    value: float = Field(allow_inf_nan=False)
    unit: str = ""
    measured_at: UtcDatetime | None = None
    note: str = ""


class EnvironmentBatchIn(BaseModel):
    readings: list[EnvironmentReadingIn] = Field(min_length=1, max_length=500)


class PersonBookingIn(BaseModel):
    kind: Literal["leave", "training", "duty"] = "leave"
    starts_at: UtcDatetime
    ends_at: UtcDatetime
    reason: str = ""


class SimulateIn(BaseModel):
    """执行前仿真：并发几个批次、从什么时候开始、是否叠加当前时间线。"""

    concurrency: int = Field(default=1, ge=1, le=20)
    start_from: UtcDatetime | None = None
    use_timeline: bool = True


class RecipeTransitionIn(Signed):
    target_state: Literal["approved", "released", "retired"]


class GoldenBatchIn(Signed):
    batch_id: str


# ---------- 实验方案 ----------

class PlanCreateIn(BaseModel):
    name: str
    # 套用模板时可以不给：取模板建议的方法
    recipe_id: str = ""
    # 方案模板：模板给缺省结构，本请求里显式给的字段优先
    template_id: str = ""
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
    # 这份提案由哪次分析运行生成（先固化数据集快照、登记分析运行，再提交提案）
    analysis_run_id: str = Field(default="", max_length=64)


class DatasetSnapshotIn(BaseModel):
    """固化一份训练数据快照。key 可选：同一实验活动里同号重发回放原快照，数据已变则拒绝。"""

    key: str = Field(default="", max_length=128)
    note: str = Field(default="", max_length=500)


class AnalysisRunIn(BaseModel):
    """一次分析 / 模型运行：输入快照、程序与模型版本、参数、随机种子、输出摘要。run_id 同号重发回放。"""

    run_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=64)
    program: str = Field(default="", max_length=128)
    program_version: str = Field(default="", max_length=64)
    model_version: str = Field(default="", max_length=128)
    params: dict[str, Any] = {}
    seed: str | int = ""
    outputs: dict[str, Any] = {}


class DecisionIn(BaseModel):
    """审批 / 审核的统一结论输入。不接受任意目标状态。"""

    conclusion: Literal["approved", "rejected"]
    reason: str = ""
    signature_id: str | None = None
    effective_from: UtcDatetime | None = None


# ---------- 实验任务 ----------

class TaskSplitIn(BaseModel):
    """怎么分批。每批最多几个样本（chunk_size，按它装满）或分几份（parts，均分）；都不填按最少批数均分
    （20 个、每批最多 8 个 → 7、7、6）。replicate：每份按方案整体执行一次（整体重复），不填时一批放得下的
    矩阵方案给了份数就按整体重复（兼容旧用法），其余按份额拆分。mode：parallel 并行（排程按设备决定先后）/
    pilot 首批验证后放行其余 / sequential 逐批顺序。"""

    chunk_size: int | None = Field(default=None, ge=1, le=96)
    parts: int | None = Field(default=None, ge=2, le=50)
    replicate: bool | None = None
    mode: Literal["parallel", "pilot", "sequential"] | None = None


class TaskCreateIn(BaseModel):
    plan_id: str
    title: str = ""
    owner_user_id: str = ""
    reviewer_user_id: str = ""
    sample_ids: list[str] = []
    due_at: UtcDatetime | None = None
    priority: int = Field(default=2, ge=1, le=5)
    note: str = ""
    # 任务树与依赖：挂在哪个父任务下、依赖哪些上游任务
    parent_id: str = ""
    depends_on: list[str] = []
    # 上游怎样才算满足：运行结束 / 数据复核通过 / 报告发布放行
    dependency_gate: Literal["run_completed", "data_validated", "released"] = "run_completed"
    # 方案超过流程每批样品位时按它拆成子任务；不传按最少批数均分、并行
    split: TaskSplitIn | None = None


class TaskDecomposeIn(TaskSplitIn):
    """拆分任务。sequential 是旧参数，等同 mode=sequential。"""

    sequential: bool = False


class TaskSplitPreviewIn(TaskSplitIn):
    """拆分预览：按方案（建任务前）或按任务（拆分前）算出每一份，不写库。"""

    plan_id: str = ""
    task_id: str = ""
    sample_ids: list[str] = []


class TaskRetestIn(BaseModel):
    """补测：在父任务下新建一个补测子任务。不填按当前短缺：按数量拆的补同样多个，
    有样本清单的补没有有效完成的那些样本，矩阵补失败样本所在的条件组。"""

    sample_count: int | None = Field(default=None, ge=1, le=96)
    sample_ids: list[str] = []
    note: str = ""


class TaskBatchesIn(BaseModel):
    """为父任务下还没建批次的子任务各建一个批次。priority 不填沿用各子任务的优先级。"""

    priority: int | None = Field(default=None, ge=1, le=5)
    note: str = ""


class ShortfallAcceptIn(BaseModel):
    """按现有结果结束、不再补测：写明原因并签名。count 不填按当前短缺。"""

    count: int | None = Field(default=None, ge=1)
    reason: str
    signature_id: str | None = None


class TaskDependenciesIn(BaseModel):
    depends_on: list[str] = []
    # 不传就保持原放行条件
    gate: Literal["run_completed", "data_validated", "released"] | None = None


class TaskMigrateIn(BaseModel):
    """把任务显式迁移到方案当前的批准版本。必须写原因，留审计。"""

    reason: str


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
    start_from: UtcDatetime | None = None
    prefer_station_id: str | None = None


class RescheduleIn(BaseModel):
    from_step: int = Field(ge=0)
    start_from: UtcDatetime


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
    start_from: UtcDatetime | None = None
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
    start_from: UtcDatetime | None = None


# ---------- 步骤运行 ----------

class StepSubmitIn(Versioned):
    """人工步骤提交。缺项不推进，所以字段值与核对勾选都要给全。"""

    form_data: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    note: str = ""
    signature_id: str | None = None


class GateDecisionIn(Signed):
    """保持中的质检关卡人工判定：放行或判不合格，都要写依据。

    逐样本关卡放行时可以指定要剔除的样本（布局孔位）；不指定就是全部放行。
    """

    conclusion: Literal["approved", "rejected"]
    reason: str
    exclude_wells: list[str] = Field(default_factory=list, max_length=384)


class SplitPlacementIn(BaseModel):
    parent_sample_id: str
    number: int = Field(ge=1, le=96)
    well: str = Field(min_length=1, max_length=20)


class SplitConfirmIn(BaseModel):
    """实体分装确认：每个母样本的每一份落在哪个孔。写了载具角色就落到批次按该角色绑定的板上。"""

    placements: list[SplitPlacementIn]
    labware_role: str = Field(default="", max_length=40)
    # 不写角色也可以落到主载具上
    use_labware: bool = False
    note: str = ""


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


class ChannelConfigIn(BaseModel):
    # 邮件收件人
    to: list[str] = Field(default_factory=list, max_length=50)
    # 报警只推严重度不低于它的（1 最严重）；不填全推
    max_severity: int | None = Field(default=None, ge=1, le=4)


class WebhookIn(BaseModel):
    name: str
    # webhook：签名 JSON；wecom / dingtalk：群机器人地址；email：不填，收件人写在 config.to
    channel: Literal["webhook", "wecom", "dingtalk", "email"] = "webhook"
    url: str = ""
    topics: list[str]
    enabled: bool = True
    config: ChannelConfigIn = Field(default_factory=ChannelConfigIn)
    # 钉钉机器人「加签」密钥（可选）；只写不读
    bot_secret: str = ""


class WebhookPatchIn(BaseModel):
    name: str | None = None
    url: str | None = None
    topics: list[str] | None = None
    enabled: bool | None = None
    config: ChannelConfigIn | None = None
    bot_secret: str | None = None
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
    occurred_at: UtcDatetime | None = None


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
    occurred_at: UtcDatetime | None = None


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


class MaterialPatchIn(Versioned):
    """改物料主数据。没传的字段不动；名称与基础单位在已有批号时锁定（服务端判）。"""

    name: str | None = None
    base_unit: str | None = None
    category: str | None = None
    cas: str | None = None
    conversions: dict[str, Quantity] | None = None
    external_ref: str | None = None
    ghs: list[str] | None = None


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
    collected_at: UtcDatetime | None = None
    parser_version: str = ""
    raw_file_id: str = ""
    instrument_serial: str = ""
    # 测出这些值的工位（可选）；服务身份须在 stations 授权范围内
    station_id: str = ""
    metrics: list[MetricValueIn]


class ManualResultIn(BaseModel):
    event_id: str
    sample_id: str = ""
    collected_at: UtcDatetime | None = None
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
    collected_at: UtcDatetime | None = None
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
    effective_from: UtcDatetime | None = None
    effective_to: UtcDatetime | None = None
    review_due: date | None = None
    category: str | None = None
    owner_id: str | None = None
    # 同编号修订时从当前生效版本复制结构化步骤（连同稳定标识）
    copy_steps: bool = False


class SopVersionPatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    file_id: str | None = None
    capability_scope: list[str] | None = None
    sample_types: list[str] | None = None
    requires_training_ack: bool | None = None
    effective_from: UtcDatetime | None = None
    effective_to: UtcDatetime | None = None
    review_due: date | None = None
    category: str | None = None
    owner_id: str | None = None


class SopDocumentPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    owner_id: str | None = None


class IslandIn(BaseModel):
    """给实验区（工位上的岛号）起名字。岛号写在工位上，这里只登记给人看的名称。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)


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
    # 报告模板：standard 完整实验报告 / summary 结果摘要 / audit 质量审计报告
    template: Literal["standard", "summary", "audit"] = "standard"


class ReportPatchIn(Versioned):
    conclusion: str | None = None
    refresh: bool = False
    template: Literal["standard", "summary", "audit"] | None = None


class PublishIn(Signed):
    pass


# ---------- 资源 ----------

# 能力极限：数值参数 [下限, 上限]，选项型参数是允许的选项（登记选项的子集），程序表按列写 {列: 极限}；
# 写法由服务端按参数规格核
LimitWindow = list[float] | list[str] | dict[str, list[float] | list[str]]


class LimitsIn(Signed):
    # 只列要改的能力：没列出的保持原样（合并写入）
    limits: dict[str, dict[str, LimitWindow]] = {}
    # 这台工位不再承接的能力：整项移除，引用它的流程随之重校验
    remove: list[str] = []
    row_version: int | None = None


class ProgramColumnIn(BaseModel):
    """程序表的一列：标识、显示名、类型（数值 / 整数 / 选项）、单位、选项、每行是否必填。"""

    key: str = Field(max_length=32)
    label: str = Field(default="", max_length=60)
    type: Literal["number", "integer", "enum"] = "number"
    unit: str = Field(default="", max_length=32)
    options: list[str] = Field(default_factory=list, max_length=50)
    required: bool = False


class ParamSpecIn(BaseModel):
    """能力参数的规格：数值、整数、选项或程序表、单位、是否必填。没登记的参数按「数值、单位未登记、必填」解释。

    选项型（enum）写 `options`：值只能是其中之一，原样作为文字下发；没有单位。
    程序表（program）写 `columns` 与 `max_rows`：值是行的列表（充放电工步、升温程序），见 domain/program.py。"""

    type: Literal["number", "integer", "enum", "program"] = "number"
    unit: str = Field(default="", max_length=32)
    required: bool = True
    options: list[str] = Field(default_factory=list, max_length=50)
    columns: list[ProgramColumnIn] = Field(default_factory=list, max_length=20)
    max_rows: int | None = Field(default=None, ge=1, le=200)


class CapabilityIn(Signed):
    id: str
    name: str
    params: dict[str, str]
    param_specs: dict[str, ParamSpecIn] = {}
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
    # 只对不关联资产的工位有意义；关联了资产以资产登记的型号为准
    model: str = ""
    channels: int = Field(default=1, ge=1, le=512)
    # 通道怎么计：batch 一个批次的一个设备步骤占 1 个；sample 批次里每个样本各占 1 个（一颗电芯一个通道）
    channel_unit: Literal["batch", "sample"] = "batch"
    limits: dict[str, dict[str, LimitWindow]] = {}
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
    # 套用设备接入模板（已发布的某一版）与这台设备自己的连接参数；空字符串表示不再按模板管理
    template_id: str | None = None
    template_connection: dict[str, Any] | None = None
    row_version: int


class AdapterCreateIn(Signed):
    """给还没接设备的工位登记适配器：手工给驱动与配置，或套一份已发布的设备接入模板。

    套模板时驱动、协议、支持标志与完整配置都由「模板 + 连接参数」算出来，这里只填这台设备自己的连接参数与凭据引用。
    """

    model_config = ConfigDict(extra="forbid")

    protocol: str = ""
    driver: str = "simulation"
    version: str = ""
    kind: Literal["simulation", "real"] = "simulation"
    config: dict[str, Any] = {}
    credential_ref: str = ""
    enabled: bool = True
    supports_hold: bool = True
    supports_abort: bool = True
    supports_query: bool = True
    supports_dedup: bool = True
    note: str = ""
    template_id: str = ""
    template_connection: dict[str, Any] = {}


class DeviceTemplateIn(BaseModel):
    """设备接入模板草稿：驱动 + 映射配置 + 连接参数示例 + 支持标志 + 验收缺省。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    name: str
    driver: str
    model: str = ""
    vendor: str = ""
    protocol: str = ""
    version: str = ""
    config: dict[str, Any] = {}
    connection: dict[str, Any] = {}
    supports: dict[str, bool] = {}
    acceptance: dict[str, Any] = {}
    note: str = ""


class DeviceTemplatePatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    driver: str | None = None
    model: str | None = None
    vendor: str | None = None
    protocol: str | None = None
    version: str | None = None
    config: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    supports: dict[str, bool] | None = None
    acceptance: dict[str, Any] | None = None
    note: str | None = None


class DeviceTemplateReleaseIn(Signed):
    row_version: int


class DeviceTemplateImportIn(BaseModel):
    """导入模板文件（ilcs-device-template/1）：界面读出文件内容后原样提交。"""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(default="", max_length=255)
    document: dict[str, Any]


class AdapterConfigCheckIn(BaseModel):
    """保存之前先检查一份适配器配置（不保存、不连设备）。"""

    model_config = ConfigDict(extra="forbid")

    driver: str
    protocol: str = ""
    config: dict[str, Any] = {}
    credential_ref: str = ""


class AcceptanceWaiveIn(Signed):
    """签名放行接入验收：检查清单证明不了的设备，现场核对后由人放行。签名针对工位与当前配置版本。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=4, max_length=2000)


class AcceptanceRequestIn(BaseModel):
    """申请一次设备接入验收。动作级要签名并写明现场批准人（DEC-02）；故障项目只随动作级一起申请。"""

    model_config = ConfigDict(extra="forbid")

    level: Literal["readonly", "physical"] = "readonly"
    faults: bool = False
    capability: str | None = None
    params: dict[str, Any] | None = None
    approval: str = Field(default="", max_length=1000)
    signature_id: str | None = None


class StationPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    # 关联了资产的工位只接受空值或与资产登记一致的值（清掉早先不一致的登记），要改型号请改资产
    model: str | None = None
    island: int | None = None
    channels: int | None = Field(default=None, ge=1, le=512)
    channel_unit: Literal["batch", "sample"] | None = None
    asset_id: str | None = None
    # 乐观并发：带上读到的版本，别人先改过就 409，不做后写覆盖前写
    row_version: int | None = None


class CapabilityPatchIn(Signed):
    name: str | None = None
    params: dict[str, str] | None = None
    param_specs: dict[str, ParamSpecIn] | None = None
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
    device_ts: UtcDatetime
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
    device_ts: UtcDatetime | None = None
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
    # 空串是主载具（装批次样本）；多块板并行时给其他板一个角色，步骤按 labware 取板
    role: str = Field(default="", max_length=40)


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


# ---------- 配液模板与实验表格导入 ----------

# 表格单元格：xlsx 里的数字是 float，文字与 csv 单元格是 str，空格子是 None
TableCell = str | float | None


class FormulationTemplateIn(BaseModel):
    """配液模板：固定步骤 + 加料阶段 + 物料类别 → 加法 + 搅拌规则 + 实验参数，config 结构见 domain/formulation.py。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    name: str
    description: str = ""
    config: dict[str, Any]


class FormulationTemplatePatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None


class FormulationPreviewIn(BaseModel):
    """按已解析的表格（parse 返回的原样表格，或界面改了实验参数后重提）生成预览，不写库。"""

    filename: str = Field(default="", max_length=200)
    table: list[list[TableCell]]
    params: dict[str, float] = {}


class FormulationImportIn(FormulationPreviewIn):
    """导入：服务端重新生成（不信任前端预览），一个事务里登记样本、建流程草稿（或沿用）与方案草稿。"""

    plan_name: str = Field(default="", max_length=200)
