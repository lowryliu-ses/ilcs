/* 服务端契约的前端镜像。只写界面真正读的字段。 */

export type User = {
  id: string;
  username: string;
  display_name: string;
  /** 主角色；权限按 `roles`（可多个）并集计算 */
  role: string;
  roles: string[];
  role_name: string;
  perms: string[];
  /** 当前组织由成员关系确定，不由请求体里的 organization_id 决定 */
  organization_id: string;
  organization_name: string;
  organization_timezone: string;
  restricted_projects: boolean;
  project_ids: string[];
  must_change_password: boolean;
  password_changed_at: string | null;
  /** 测试环境开关：系统管理员可审批本人内容 */
  admin_self_approval?: boolean;
  /** 测试环境开关：系统管理员可级联强制删除 */
  admin_force_delete?: boolean;
  /** 启用的可选模块（如 formulation 配液模板）；界面据此决定出不出对应菜单 */
  modules?: string[];
};

/** 执行门：`open`/`reasons` 是全站（联锁、执行器）；`blocked_stations` 是单台设备的失联 / 心跳超时，只挡用到它的批次。 */
export type Gate = {
  open: boolean;
  reasons: string[];
  blocked_stations?: Record<string, string>;
  /** 配置变更后还欠接入验收的工位（失联时也列着：失联是首要原因，但验收同样还欠着） */
  acceptance_pending?: Record<string, string>;
  degraded: string[];
  checked_at: string;
};

/** 开跑检查项。`state` 区分通过、阻塞与不适用；`ok` 表示「不挡下发」。 */
export type Check = {
  key: string;
  label: string;
  ok: boolean;
  state?: 'pass' | 'warn' | 'blocked' | 'not_applicable';
  detail: string;
};

export type NextAction = { who: string; what: string; why: string };

/** 批次状态的中文名，与服务端 BatchService.STATE_LABEL 一致。 */
export const BATCH_STATE_LABEL: Record<string, string> = {
  planned: '计划', scheduled: '已排程', running: '运行中', paused: '已保持',
  fault: '故障', aborting: '终止中', aborted: '已终止', done: '已完成',
};

/** 流程状态的中文名，与服务端 recipe_service.STATE_LABEL 一致；接口带了 state_label 时以接口为准。 */
export const RECIPE_STATE_LABEL: Record<string, string> = {
  draft: '草稿', review: '评审中', approved: '已批准', released: '已发布', retired: '已退役',
};

/** 方案结构状态的中文名，与服务端 plan_service.STATE_LABEL 一致。 */
export const PLAN_STATE_LABEL: Record<string, string> = { draft: '草稿', locked: '矩阵已锁定' };

export const PLAN_TYPE_LABEL: Record<string, string> = {
  matrix: '矩阵实验', single_condition: '单条件样本实验', commissioned_test: '委托检测',
};

export type BatchSummary = {
  id: string;
  state: string;
  state_label: string;
  recipe_id: string;
  recipe_name: string;
  version: string;
  plan_id: string;
  plan_version: number;
  /** 绑定的实验任务。批次与任务一一对应，不留第二条数据链。 */
  task_id: string;
  /** 一个方案分多批执行时：父任务、本批是父任务的哪一份、是不是补测 */
  task_parent_id?: string;
  task_portion_label?: string;
  task_purpose?: string;
  priority: number;
  operator: string;
  note: string;
  failure_reason: string;
  current_step: number;
  step_count: number;
  sample_count: number;
  sample_done: number;
  resource_demand: {
    total: number;
    needs_station: number;
    device: number;
    manual: number;
    wait: number;
    review: number;
  };
  row_version: number;
  starts_at: string | null;
  ends_at: string | null;
  created_at: string;
  held_at: string | null;
  current_station: string | null;
  next_action: NextAction;
  delete_blockers: string[];
};

export type StepKindName = 'device' | 'manual' | 'wait' | 'review' | 'gate' | 'split' | 'merge' | 'branch' | 'subflow' | 'notify';

/** 条件分支配置。出口按顺序匹配，第一个满足的生效；loop_to 表示回到上游某一步重做。 */
export type BranchConfig = {
  mode?: 'measure' | 'form' | 'manual';
  source_step_id?: string;
  field?: string;
  cases?: BranchCase[];
  default?: string;
  max_loops?: number;
  /** 按样本分流：每个样本按自己孔位上的读数走自己的出口（只按测量值、不回环），各条路只处理分到的样本 */
  per_sample?: boolean;
};
export type BranchCase = {
  key: string;
  label: string;
  min?: number | null;
  max?: number | null;
  equals?: string;
  loop_to?: string;
};
export type StepTimeout = { minutes: number; action: 'alarm' | 'fail' | 'skip' };
export type SubflowGroup = { step_id: string; name: string; recipe_id: string; recipe_name: string; version: string };

/** 节点对应的 SOP 指导：从批次固化的 SOP 快照取。index 为 0 表示只有生成流程时抄下的说明 */
export type SopGuide = {
  index: number;
  title: string;
  instructions: string;
  checks: string[];
  /** 节点引用的 SOP 步骤在批次采用的版本里对不上 */
  mapping_broken?: boolean;
  message?: string;
};

export type StepRow = {
  index: number;
  step_id: string;
  kind: StepKindName;
  kind_label: string;
  sop_guide?: SopGuide | null;
  /** 人工 / 等待 / 审核节点默认不占工位，除非显式声明 */
  needs_station: boolean;
  name: string;
  cap: string;
  cap_name: string;
  params: Record<string, number>;
  /** 按哪版设备方法执行（冻结在批次快照里） */
  method?: { id: string; code: string; version: number; name: string; program: string } | null;
  dur: number;
  hard: { from?: string; maxGapMin?: number } | null;
  form: FormField[];
  wait_for: { mode?: string; event?: string };
  review_role: string;
  branch: BranchConfig;
  when: Record<string, string>;
  after: string[];
  skippable: boolean;
  timeout: StepTimeout | null;
  groups: SubflowGroup[];
  recovery: Recovery;
  station_id: string | null;
  planned_start: string | null;
  planned_end: string | null;
  transfer_station_id: string | null;
  /** 这一步一并占用的协同工位；声明的协同能力；用哪块载具（角色）；拆分配置 */
  assist_station_ids?: string[];
  assist?: string[];
  labware?: string;
  split?: { count?: number; child_type?: string; mode?: 'logical' | 'physical' };
  /** 非空表示计划时间窗只是预测（分支未定的下游、冻结期之后） */
  forecast_reason?: string;
  checkpoint_id: string | null;
  actual_end: string | null;
  /** 同一步的历次尝试；审核退回会生成新的一次 */
  attempts: StepRunRow[];
  run: StepRunRow | null;
  state: string;
};

export type Recovery = {
  maxHoldMin?: number;
  pausable?: boolean;
  hold?: string;
  retryable?: boolean;
  sideEffect?: string;
  verify?: string[];
  /** 做完（或可能做过）后设备转为待清洗，清洗确认前不给别的批次用 */
  cleanAfter?: boolean;
};

/** 运行分配：样本 × 批次 × 孔位 × 条件组。物理样本见下方 `SampleRow`。 */
export type AssignmentRow = {
  id: string;
  physical_sample_id: string;
  barcode: string;
  container_id: string;
  well: string;
  position: number;
  condition_group: string;
  condition_label: string;
  repeat: number;
  levels: (number | string)[] | null;
  is_control: boolean;
  state: string;
  flag_note: string;
};

/** 预留占用。数量一律是十进制字符串——前端不做浮点运算，直接显示服务端的值。 */
export type ReservationRow = {
  id: number;
  batch_id: string;
  lot_id: string;
  material: string;
  release: string;
  qty: string;
  unit: string;
  state: string;
  consumed_qty: string;
  loss_qty: string;
  released_qty: string;
  issued_qty: string;
  returned_qty: string;
  outstanding: string;
  issued_outstanding: string;
  row_version: number;
  delivered_qty: number;
};

export type BatchDetail = BatchSummary & {
  snapshot: {
    id: string;
    name: string;
    version: string;
    plate: number;
    risk: string;
    steps: unknown[];
    bom: BomItem[];
    sop_version_id?: string;
  };
  plan_snapshot: { id: string; name: string; goal: string; repeats: number; factors: Factor[] };
  /** 批次固化的 SOP 版本、附件与结构化步骤：执行人照着它干活 */
  sop_snapshot: SopSnapshot;
  steps: StepRow[];
  allocations: Allocation[];
  samples: AssignmentRow[];
  reservations: ReservationRow[];
  inventory_ledger: LedgerLine[];
  step_runs: StepRunRow[];
  workflow_events: WorkflowEventRow[];
  commands: CommandRow[];
  /** 绑定的主载具；未绑定时为空，此时不做位置追踪与转运 */
  labware: LabwareRow | null;
  /** 绑定的全部载具（多块板并行时按角色列出，主载具角色为空、排在前面） */
  labware_all: LabwareRow[];
  checkpoints: { id: string; step_index: number; state: string; created_at: string; payload: Record<string, unknown> }[];
  alarms: { id: string; severity: number; state: string; message: string; condition_active: boolean }[];
  audit: AuditRow[];
  telemetry: {
    station_id: string;
    metric: string;
    setpoint: number | null;
    value: number | null;
    quality: string;
    origin: string;
    device_ts: string;
  }[];
  gate: Gate;
  preflight: Preflight | null;
  can_control: boolean;
  signals: BatchSignalRow[];
  graph_mode: boolean;
  subflows: SubflowGroup[];
};

export type BatchSignalRow = {
  id: string;
  batch_id: string;
  name: string;
  payload: Record<string, unknown>;
  source: string;
  source_label: string;
  received_at: string | null;
  consumed_by_run_id: string;
  consumed_at: string | null;
};

export type Preflight = {
  checks: Check[];
  ok: boolean;
  blocked: Check[];
  summary: { total: number; passed: number; blocked: number; not_applicable: number };
  first_station_id: string | null;
  sample_count: number;
  pallet_code: string;
  resource_checks: {
    step_index: number;
    step_id: string;
    step_name: string;
    applicable: boolean;
    ok: boolean;
    reasons: string[];
  }[];
};

export type ChannelUnit = 'batch' | 'sample';

export const CHANNEL_UNIT_LABEL: Record<ChannelUnit, string> = {
  batch: '按批次（一个设备步骤占 1 个通道）',
  sample: '按样本（每个样本占 1 个通道）',
};

export type Allocation = {
  step_index: number;
  station_id: string;
  /** assist：协同资源，与主设备同起同止一并占用 */
  kind: 'work' | 'transfer' | 'clean' | 'assist';
  /** 占几份通道：按样本计通道的工位是批次的样本数，其余 1 */
  units?: number;
  starts_at: string;
  ends_at: string;
  /** 预测窗口（分支未定的下游、冻结期之后）；false 为承诺窗口 */
  forecast?: boolean;
  forecast_reason?: string;
};

export type CommandRow = {
  /** queued | maybe_sent | delivered | unreachable。maybe_sent 禁止盲目重试 */
  delivery_state?: string;
  /** 设备给出的结论，与投递事实分开：failed 明确失败（已停下）；unknown 设备收到却说不清做成没有，
   *  与 maybe_sent 一样保留占用、禁止盲目重试 */
  outcome?: string;
  step_run_id?: string;
  id: string;
  batch_id?: string;
  type: string;
  state: string;
  station_id: string;
  step_index: number;
  checkpoint_id: string;
  error: string;
  created_at: string;
  updated_at?: string;
  /** 前置指令（转运）；它完成前本指令不投递 */
  after_command_id?: string;
  /** 保持 / 终止要停的动作，或续跑 / 重试接续的那个被保持的动作 */
  target_command_id?: string;
  /** 转运搬的是哪块板 */
  labware_id?: string;
  /** 前馈参数的求值记录 */
  bindings?: BindingRecord[];
  /** 这条动作一并占用的协同工位 */
  assist_station_ids?: string[];
};

export type RecoveryOption = { id: string; label: string; allowed: boolean; reason: string; impact: string };

export type RecoveryEvaluation = {
  batch_id: string;
  hold_reason: string;
  held_at: string | null;
  step_index: number;
  step_name: string;
  capability_name: string;
  hold_state: string;
  verify: string[];
  preconditions: { key: string; label: string; ok: boolean; detail: string }[];
  ready: boolean;
  options: RecoveryOption[];
  gate: Gate;
  /** 当前出问题的步骤能不能跳过（方法标了可跳过、且没有结果未知的指令） */
  skip: { step_id: string; allowed: boolean; reason: string };
  /** 可以「从这一步重做」的步骤：当前步骤及其上游 */
  rerun_targets: { step_id: string; index: number; name: string }[];
};

export type BomItem = { material: string; qty: number; unit: string };

/** 物料主数据（GET /materials）。批号、BOM、步骤投料物料都按 name 对应 */
export type MaterialRow = {
  id: string;
  code: string;
  name: string;
  base_unit: string;
  category: string;
  cas: string;
  /** 单位 → 1 单位折合多少基础单位（十进制文字） */
  conversions: Record<string, string>;
  external_ref: string;
  ghs: string[];
  /** active | retired */
  state: string;
  lot_count: number;
  /** 已有批号时锁定的字段（name、base_unit）：批号、预留与消耗按名称与单位对账 */
  locked_fields: string[];
  row_version: number;
  updated_at: string;
};

export type Factor = {
  name: string;
  /** 水平的单位；作用的设备参数单位不同时，建批次按物料登记的摩尔质量 / 密度 / 浓度换算（mmol → mg、eq → μL） */
  unit: string;
  levels: (number | string)[];
  material?: { name: string; unit: string; per: number };
  /** 作用的设备参数：水平按孔位覆盖该设备步骤的参数，随指令下发 */
  target?: { step_id: string; param: string };
  /** unit 为 eq（当量）时的基准：另一个因子（限量试剂的物质的量），或一个固定的物质的量 */
  basis?: { factor?: string; amount?: number; unit?: string };
};

export type FactorTargetOption = {
  step_id: string;
  step_name: string;
  capability: string;
  /** type 为 enum 时是选项型参数：因子水平只能从 options 里挑 */
  params: { name: string; label?: string; unit: string; type?: 'number' | 'integer' | 'enum'; options?: string[] }[];
};

export type RecipeSummary = {
  id: string;
  name: string;
  /** 关联的受控 SOP 版本；空表示未关联 */
  sop_version_id?: string;
  version: string;
  state: string;
  state_label: string;
  owner: string;
  updated: string;
  plate: number;
  risk: string;
  design: string;
  golden_batch_id: string;
  parent: string;
  needs_revision: boolean;
  valid: boolean;
  step_count: number;
  /** 关键路径（分钟）；有子流程时按展开后的步骤算 */
  critical_path_min: number;
  delete_blockers: string[];
  /** 乐观并发版本；编辑保存与审批签名都绑定它 */
  row_version: number;
  author_user_id: string;
  submitted_by: string;
};

export type SimulationResult = {
  recipe_id: string;
  version: string;
  content_hash: string;
  at: string;
  ok: boolean;
  concurrency: number;
  checks: Check[];
  /** 每种分支走法：所选出口、执行步骤数、关键路径 */
  paths: { choices: string[]; steps: number; duration_min: number }[];
  /** 回环出口最坏情况下（回满上限）额外增加的时长 */
  loops: { branch_step_id: string; case: string; label: string; max_loops: number; body_steps: string[]; extra_min_worst: number }[];
  station_load_min: Record<string, number>;
  summary: { blocked: number; warn: number; pass: number };
};

export type RecipeDetail = RecipeSummary & {
  /** 最近一次仿真结论（提交评审与批准时服务端会重跑可执行性检查） */
  simulation: SimulationResult | Record<string, never>;
  /** 最近一次仿真是否针对当前内容（步骤与 BOM 未再改动） */
  simulation_current: boolean;
  steps: RecipeStep[];
  /** 用过的步骤标识（含已删除的）：编辑器本地分配新标识时必须避开 */
  used_step_ids: string[];
  bom: BomItem[];
  history: { v: string; state: string; note: string; by: string; at: string }[];
  diff: [string, string][];
  validation: {
    index: number;
    name: string;
    cap: string;
    cap_name: string;
    params: Record<string, number>;
    dur: number;
    fits: string[];
    ok: boolean;
    issues: string[];
    blockers: string[];
  }[];
  checks: Check[];
  recovery_by_capability: Record<string, Recovery>;
  plans: { id: string; name: string; state: string }[];
  /** 已关联 SOP 版本的摘要；未关联时为 null */
  sop: {
    sop_version_id: string;
    code: string;
    title: string;
    version: string;
    state: string;
    status: SopStatus;
    status_label: string;
    file_id: string;
    file_checksum: string;
    requires_training_ack: boolean;
    capability_scope: string[];
    /** 新批次实际会用的版本：关联版本被取代时是同编号的当前生效版本；没有生效版本时为空 */
    current_version_id: string;
    current_version: string;
  } | null;
};

/** 方法步骤。字段按 kind 分支适用：设备看 cap/params，人工看 form，
    等待看 wait_for，审核看 review_role。step_id 稳定不复用。 */
export type EnvironmentRequirement = { metric: string; min?: number | null; max?: number | null; zone?: string };

export type EnvironmentReadingRow = {
  id: string;
  zone: string;
  metric: string;
  metric_label: string;
  value: number;
  unit: string;
  source: string;
  measured_at: string;
  recorded_by: string;
  note: string;
  age_min: number;
  stale: boolean;
};

export type RecipeStep = {
  step_id?: string;
  kind?: StepKindName;
  name: string;
  /** 对应的 SOP 步骤序号（从 1 起），只作显示与旧数据回退 */
  sop_step?: number;
  /** 对应的 SOP 步骤的稳定标识：批次页按它把 SOP 说明与核对项带给执行人，新版本插入步骤也对得上 */
  sop_step_key?: string;
  /** 由 SOP 生成流程时抄下的说明 */
  sop_instructions?: string;
  cap: string;
  /** 固定参数：数值、选项型参数的选项文字，或程序表的行；'' 是界面上还没填 */
  params: Record<string, number | string | ProgramRow[]>;
  dur: number;
  hard?: { from: string; maxGapMin: number };
  form?: FormField[];
  wait_for?: { mode?: 'duration' | 'event'; event?: string };
  review_role?: string;
  requires_signature?: boolean;
  requires_sample_check?: boolean;
  consumes_materials?: boolean;
  /** 这一步投的是哪种物料（与 BOM、批号上的物料名称一致）。BOM 没列时用量由实验方案按样本给出 */
  material?: string;
  /** 投料用量取自哪个能力参数（仅设备步骤）；不填时执行器按物料单位推断 */
  material_param?: string;
  /** 一步投几种料（仅设备步骤，整线一个任务投完一瓶的全部组分）：按加料顺序，每种料写明用量参数；与 material 二选一 */
  materials?: { material: string; param: string }[];
  /** 人工步骤占哪台工位（指定一台，或某能力的任一台）；等待步骤 holds_station：样本留在上一步的设备里 */
  resource?: { station?: string; capability?: string; holds_station?: boolean };
  /** 人工步骤要求执行人具备的资质：SOP 编号、安全操作资质编号 */
  qualification?: { sop?: string; safety?: string };
  /** 按样本执行：只处理在 dosed 那一步真加了料的样本；写了 then_any 时之后还要再加其中一种 */
  applies_to?: { dosed?: string; then_any?: string[] };
  /** 质检关卡：读测量来源步骤回执里的 field，按 min/max 判定 */
  gate?: {
    source_step_id?: string;
    field?: string;
    min?: number | null;
    max?: number | null;
    scope?: 'batch' | 'sample';
    on_fail?: 'rework' | 'scrap' | 'hold';
    rework_to?: string;
    max_rework?: number;
  };
  /** 样本拆分：每个样本拆出 count 个子样本；physical 时要按实际分装孔位确认后才推进 */
  /** count_from：份数按样本取（方案因子的水平，或上游设备每孔的读数），取不到用 count */
  split?: {
    count?: number; child_type?: string; mode?: 'logical' | 'physical';
    count_from?: { factor?: string; source_step_id?: string; field?: string };
  };
  /** 样本合并：同一条件组（condition）或全部（all）的在用样本合成一个新样本，谱系指回全部母样 */
  merge?: { by?: 'condition' | 'all'; child_type?: string };
  /** 协同资源：执行期间与主设备一并占用的能力（机械臂、配套设备、放置位） */
  assist?: string[];
  /** 用哪块载具（按角色）；空为主载具 */
  labware?: string;
  /** 前驱步骤（依赖图）。任何一步声明了它，流程按依赖图推进；未声明的步骤依赖上一行 */
  after?: string[];
  /** 前驱是条件分支时，本步在它的哪个出口上 */
  when?: Record<string, string>;
  branch?: BranchConfig;
  subflow?: { recipe_id?: string };
  /** 设备步骤引用的设备方法（流程管做什么，方法管怎么做） */
  method?: StepMethodRef;
  /** 取自上游结果的参数（前馈）：参数键 → 来源、单位、系数与预期范围。与固定参数二选一 */
  bindings?: Record<string, ParamBinding>;
  /** 消息通知节点：发一条 flow.notify 对外事件 */
  notify?: { message?: string; channel?: string };
  timeout?: StepTimeout;
  /** 环境要求：区域 × 指标的最新读数须在范围内（区域不写取这一步分到的工位） */
  environment?: EnvironmentRequirement[];
  /** 方法作者同意运行时可以跳过这一步 */
  skippable?: boolean;
  groups?: SubflowGroup[];
};

export type PlanSummary = {
  id: string;
  name: string;
  plan_type: string;
  plan_type_label: string;
  recipe_id: string;
  project_id: string;
  owner: string;
  /** 矩阵锁定：draft | locked。结构冻结，不代表已审批。 */
  state: string;
  state_label: string;
  /** 审批：draft | review | approved | rejected。与锁定分列。 */
  approval_state: string;
  approval_label: string;
  version: number;
  approved_version: number | null;
  created: string;
  goal: string;
  repeats: number;
  layout: string;
  seed: number;
  sample_ids: string[];
  /** 指定的样本：fresh 只用一次 / continue 接着用上一步的产物（多步合成） */
  sample_policy?: 'fresh' | 'continue';
  required_metrics: string[];
  resource_requirements: Record<string, unknown>[];
  method_version: string;
  condition_count: number;
  sample_count: number;
  batches: string[];
  task_count: number;
  row_version: number;
  reject_reason: string;
  is_matrix: boolean;
  delete_blockers: string[];
};

/** 任务中心与批次创建都读方案列表，这里给个短名。 */
export type PlanRow = PlanSummary;

export type ApprovalLevel = {
  level: number;
  label: string;
  assignee_id: string;
  assignee_name: string;
  decided_by: string;
  decided_by_name: string;
  decided_at: string | null;
  conclusion: '' | 'approved' | 'rejected';
  reason: string;
};

export type PlanTemplateRow = {
  id: string;
  name: string;
  description: string;
  plan_type: string;
  plan_type_label: string;
  recipe_id: string;
  body: Record<string, unknown>;
  source_plan_id: string;
  retired: boolean;
};

export type DiffRow = { field: string; label: string; before: string; after: string };

export type PlanDetail = PlanSummary & {
  factors: Factor[];
  control: { label: string; cond: (number | string)[] } | null;
  conditions: { group: string; levels: (number | string)[]; label: string; is_control: boolean }[];
  layout_preview: { well: string; group: string; repeat: number; label: string; is_control: boolean }[];
  target_options?: FactorTargetOption[];
  /** 闭环：显式设计点（非空时条件就是这些点）、设计空间、来源方案与轮次 */
  design_points: (number | string)[][];
  design_space: DesignSpace;
  parent_plan_id: string;
  round_no: number;
  checks: Check[];
  lockable: boolean;
  /** 按流程每批样品位分几批、每批几个样本（矩阵还有每批几次重复）；超过一批的方案建任务时自动拆分 */
  batch_plan?: {
    total: number;
    capacity: number;
    batches: number;
    sizes: number[];
    repeats: number[] | null;
    conditions: number | null;
    split: boolean;
    detail: string;
    error: string;
  };
  materials: {
    source: string;
    material: string;
    unit: string;
    qty: number;
    available: string;
    lots: string[];
    ok: boolean;
  }[];
  versions: {
    id: string;
    version: number;
    state: string;
    state_label: string;
    author_name: string;
    approver_name: string;
    approved_at: string | null;
    reject_reason: string;
    created_at: string;
    approvals?: ApprovalLevel[];
  }[];
  /** 当前版本的逐级审批进度 */
  approvals: ApprovalLevel[];
  metrics: { id: string; code: string; name: string; version: string; unit: string; value_type: string }[];
  audit: AuditRow[];
};

/** 工位台账上显示的关联资产摘要。校准、状态、容量都只读，改动去「仪器设备」 */
export type StationAsset = {
  id: string;
  asset_no: string;
  name: string;
  model: string;
  state: string;
  capacity: number;
  calibration_applicable: boolean;
  calibration_exempt_reason: string;
  calibration_valid: boolean;
  calibration_due: string | null;
  unavailable_reasons: string[];
};

/** 实验区：工位上的岛号 + 给人看的名称（没起名为空串），带在用工位数 */
export type IslandRow = { id: number; name: string; stations: number };

export type StationRow = {
  id: string;
  island: number;
  name: string;
  /** 有效型号：关联了资产取资产登记的型号，设备方法按它匹配工位 */
  model: string;
  model_source: 'asset' | 'station';
  /** 工位上早先登记、与资产不一致的型号；设备方法已按资产型号匹配，需要人核对 */
  model_conflict: string;
  status: string;
  /** 并行通道数。按批计：同一时刻能同时承接几个批次的设备步骤，一个批次的一个设备步骤占 1 个；
   *  按样本计：批次里每个样本各占 1 个（一颗电芯占一个物理通道的充放电柜） */
  channels: number;
  channel_unit: ChannelUnit;
  clean: boolean;
  dirty_batch_id: string;
  limits: Record<string, Record<string, LimitWindow>>;
  retired: boolean;
  retire_blockers: string[];
  asset_id: string;
  asset: StationAsset | null;
  adapter: AdapterRow | null;
  /** 乐观并发版本：台账、极限、就绪状态的修改都带上它 */
  row_version: number;
};

export type AdapterRow = {
  kind: 'simulation' | 'real';
  driver: string;
  protocol: string;
  version: string;
  config: Record<string, unknown>;
  credential_ref: string;
  credential_configured: boolean;
  config_version: number;
  enabled: boolean;
  row_version: number;
  updated_at: string;
  status: string;
  connected: boolean;
  accepts_commands: boolean;
  site_interlock: boolean;
  dedup_count: number;
  last_heartbeat: string;
  heartbeat_age_sec: number;
  current_command_id: string;
  note: string;
  capabilities: { hold: boolean; abort: boolean; query: boolean; dedup: boolean };
  unsupported_note: string;
  /** 参与自动流程（接指令）；映射驱动只配了点表的是「只读写点位」 */
  tasks?: boolean;
  /** 登记了点表：可以读点、手动写声明了可写的点 */
  points?: boolean;
  /** 驱动自报（或按登记配置）的设备身份与方法目录 */
  catalog?: AdapterCatalog;
  /** 配置变更后的接入验收闸门 */
  acceptance?: AcceptanceGate;
  /** 驱动在 ILCS 之外的设备服务（sila2_v1 接驱动宿主）：最近一次报的驱动与配置摘要、接入验收批准的那份 */
  driver_info?: DeviceDriverReport;
  approved_driver?: DeviceDriverReport;
  /** 报的和批准的对不上：驱动项目里改过、还没通过接入验收 */
  driver_changed?: boolean;
  /** 这次驱动变更还没有人签名批准（批准过的等接入验收出结论） */
  driver_awaiting_approval?: boolean;
  driver_approval?: { config_digest?: string; plugin?: string; approved_by?: string; approved_at?: string; reason?: string };
  /** 套用的设备接入模板（哪一版、有没有更新的发布版） */
  template?: AdapterTemplate | null;
  /** 这台设备自己的连接参数（只在受 station.edit 保护的详情接口里有） */
  template_connection?: Record<string, unknown>;
};

/** 点表里的一个点：读到的值，或读不到的原因 */
export type PointReading = {
  name: string;
  label: string;
  unit: string;
  writable: boolean;
  min: number | null;
  max: number | null;
  /** 任务用的控制信号（启动、状态、复位、指令号）：不能手动写 */
  control: boolean;
  value: number | boolean | string | null;
  error: string;
};

export type PointsListing = {
  station_id: string;
  driver: string;
  config_version: number;
  tasks: boolean;
  read_at: string;
  points: PointReading[];
};

/** 一次手动写点：签名申请、执行器先读、写、再回读 */
export type PointWriteRow = {
  id: string;
  station_id: string;
  point: string;
  value: number | boolean | string | null;
  reason: string;
  requested_by: string;
  config_version: number;
  state: 'queued' | 'running' | 'done' | 'failed' | 'unknown' | 'cancelled';
  state_label: string;
  before: number | boolean | string | null;
  after: number | boolean | string | null;
  matches: boolean | null;
  error: string;
  created_at: string | null;
  finished_at: string | null;
};

/** 设备服务在 DeviceInfo.Driver 里报的驱动插件与配置摘要 */
export type DeviceDriverReport = {
  plugin?: string;
  plugin_version?: string;
  host_version?: string;
  config_version?: string;
  config_digest?: string;
  offline_after_sec?: number;
  reported_at?: string;
};

export type AcceptanceGate = {
  /** '' 不欠 / readonly 只读级 / physical 动作级 */
  required: '' | 'readonly' | 'physical';
  required_label: string;
  reason: string;
  accepted_config_version: number | null;
  accepted_run_id: string;
};

export type AdapterTemplate = {
  id: string;
  code?: string;
  revision?: number;
  name?: string;
  state?: string;
  state_label?: string;
  latest_id?: string | null;
  latest_revision?: number | null;
  outdated?: boolean;
  missing?: boolean;
};

export type AcceptanceCheck = {
  key: string;
  label: string;
  state: 'pass' | 'fail' | 'skip';
  state_label: string;
  detail: string;
};

/** 一次设备接入验收：执行器执行，报告入库、只追加 */
export type AcceptanceRun = {
  id: string;
  station_id: string;
  level: 'readonly' | 'physical';
  level_label: string;
  faults: boolean;
  capability: string;
  params: Record<string, number>;
  trigger: string;
  trigger_label: string;
  state: 'queued' | 'running' | 'done' | 'error' | 'cancelled';
  state_label: string;
  ok: boolean | null;
  simulator: boolean;
  driver: string;
  protocol: string;
  config_version: number;
  config_digest: string;
  identity: { vendor?: string; firmware?: string; reported_model?: string };
  template: { id: string; code: string; revision: number } | null;
  counts: { pass: number; fail: number; skip: number };
  error: string;
  requested_by: string;
  approval: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  waiting_for?: number;
  checks?: AcceptanceCheck[];
  report_md?: string;
};

/** 申请验收时的缺省能力与参数：设备接入模板的验收缺省 → 适配器配置里的 acceptance → 工位极限中点 */
export type AcceptanceDefaults = { capability: string; params: Record<string, unknown>; source: 'template' | 'config' | 'limits' };

export type AcceptanceListing = {
  station_id: string;
  gate: AcceptanceGate;
  runs: AcceptanceRun[];
  defaults?: AcceptanceDefaults;
};

/** 驱动目录：驱动自己声明的配置项，界面据此出表单、保存前据此校验 */
export type DriverField = {
  name: string;
  label: string;
  /** scalar：文本 / 数值 / 是否都行（写入值、常量）；any：任意 JSON（REST 请求体） */
  type: 'string' | 'integer' | 'number' | 'boolean' | 'object' | 'array' | 'scalar' | 'any';
  type_label: string;
  required: boolean;
  /** 每台设备自己的连接参数：设备模板里不写死，套用时由工位填 */
  connection: boolean;
  hint: string;
  /** object：固定的几个键 */
  fields?: DriverField[];
  /** object：任意键 → 同一种值（点表、能力映射、状态映射） */
  entries?: DriverField;
  /** array：每一项 */
  items?: DriverField;
  /** 只能取这几个值 */
  options?: string[];
  /** 值是别处登记的名字：points（点名）/ capabilities（能力） */
  ref?: 'points' | 'capabilities';
  /** 表的键从哪来：points / capabilities / params（当前能力的参数）/ options（当前参数的选项） */
  key_ref?: 'points' | 'capabilities' | 'params' | 'options';
  key_options?: string[];
  key_label?: string;
  /** 往下走时记住表的键是哪项能力 / 哪个参数，给下层的 key_ref 用 */
  scope?: 'capability' | 'param';
  /** 只填这一个键时可以简写成它的值（"sp_temp" 就是 {"point": "sp_temp"}） */
  shorthand?: string;
  /** array：只有一项时也可以不写成列表 */
  single?: boolean;
};

export type DriverInfo = {
  key: string;
  label: string;
  protocol: string;
  summary: string;
  ledger: string;
  fields: DriverField[];
  connection_keys: string[];
  credential: string;
  supports: { hold: boolean; abort: boolean; query: boolean; dedup: boolean };
  template: Record<string, unknown>;
  /** 每项能力在这个驱动里的起步写法（按能力字典的参数生成），表单里加一项能力时照它起步 */
  capability_examples?: Record<string, unknown>;
};

export type ConfigCheck = { ok: boolean; problems: string[]; warnings: string[] };

/** 设备接入模板：一类设备怎么接，按修订号管理，发布要另一个人签名 */
export type DeviceTemplateRow = {
  id: string;
  code: string;
  revision: number;
  name: string;
  model: string;
  vendor: string;
  driver: string;
  driver_label: string;
  protocol: string;
  version: string;
  state: 'draft' | 'released' | 'retired';
  state_label: string;
  digest: string;
  connection_keys: string[];
  note: string;
  created_by_name: string;
  released_by_name: string;
  created_at: string | null;
  released_at: string | null;
  row_version: number;
  source: { kind?: string; file?: string; from?: string; revision?: number };
  usage: { stations: number; outdated: number };
  config?: Record<string, unknown>;
  connection?: Record<string, unknown>;
  supports?: Partial<Record<'hold' | 'abort' | 'query' | 'dedup', boolean>>;
  acceptance?: { capability?: string; params?: Record<string, number> };
  check?: ConfigCheck;
  stations?: {
    station_id: string;
    station_name: string;
    template_id: string;
    revision: number;
    latest_revision: number | null;
    outdated: boolean;
    acceptance_required: string;
    config_version: number;
  }[];
  revisions?: { id: string; revision: number; state: string; state_label: string; released_at: string | null }[];
};

/** 工位能套用的模板（已发布的，适用型号一致的排在前面） */
export type TemplateOption = {
  id: string;
  code: string;
  revision: number;
  name: string;
  model: string;
  driver: string;
  matches_model: boolean;
  connection_keys: string[];
  connection: Record<string, unknown>;
};

export type AdapterCatalog = {
  vendor: string;
  firmware: string;
  reported_model: string;
  /** program 为「*」表示接受任意设备端程序（模拟器） */
  methods: { program: string; name: string; capability: string }[];
  commands: string[];
  /** device 设备自报；config 按登记配置；none 没有目录 */
  described_from: '' | 'device' | 'config' | 'none';
  described_at: string | null;
};

/** 设备方法的参数规则。数值参数：缺省值与允许范围；选项型参数：缺省选项与允许的选项（能力登记选项的子集） */
export type MethodParamRule = {
  default?: number | string | ProgramRow[] | null; min?: number | null; max?: number | null; unit?: string; options?: string[];
};
export type MethodOutputRule = {
  key: string;
  label?: string;
  unit?: string;
  lo?: number | null;
  hi?: number | null;
  required?: boolean;
  /** 关联的检测指标：设备回报这个值时按样本写成该指标的检测结果（进数据审核）；空 = 只进检查点 */
  metric_id?: string;
  /** series：设备回报一条曲线（{x, y}），上下限对 y；空 = 一个数 */
  kind?: '' | 'series';
};

/** 设备方法：能力 + 适用型号 + 设备端程序 + 参数范围 + 输出规则，按版本管理 */
export type DeviceMethodRow = {
  id: string;
  code: string;
  version: number;
  name: string;
  capability_id: string;
  capability_name: string;
  instrument_models: string[];
  program: string;
  params: Record<string, MethodParamRule>;
  outputs: MethodOutputRule[];
  dur_min: number;
  state: 'draft' | 'released' | 'retired';
  state_label: string;
  note: string;
  created_by: string;
  created_at: string | null;
  released_by: string;
  released_at: string | null;
  issues: string[];
  row_version: number;
  used_by: { id: string; name: string; version: string; state: string }[];
  versions?: { id: string; version: number; state: string; state_label: string; released_at: string | null }[];
};

/** 设备步骤引用的方法。编辑器里存一份已发布版本的摘要（发布后不可改），服务端建批次时按编号重取并冻结 */
export type StepMethodRef = {
  id: string;
  code?: string;
  version?: number;
  name?: string;
  program?: string;
  instrument_models?: string[];
  params?: Record<string, MethodParamRule>;
  /** 数据输出规则（快照里冻结）：前馈以设备步骤为来源时，字段与单位按它核对 */
  outputs?: MethodOutputRule[];
};

export type AdapterTestResult = {
  ok: boolean;
  station_id: string;
  contract: {
    kind: string;
    protocol: string;
    version: string;
    capabilities: string[];
    supports_hold: boolean;
    supports_abort: boolean;
    supports_query: boolean;
    supports_dedup: boolean;
    note: string;
  };
  health: Record<string, unknown>;
};

/** 能力参数的规格：数值 / 整数、单位、是否必填。没登记的按「数值、单位未登记、必填」解释 */
/** 程序表的一列：数值 / 整数 / 选项；required 表示每行都要写 */
export type ProgramColumn = {
  key: string; label?: string; type?: 'number' | 'integer' | 'enum'; unit?: string; options?: string[]; required?: boolean;
};

/** 程序表的一格：具体的数或选项，或引用本步另一个数值参数（下发前代入） */
export type ProgramCell = number | string | { param: string };
export type ProgramRow = Record<string, ProgramCell>;

/** 能力参数的规格。选项型（enum）写 options：值只能是其中之一，原样作为文字下发，没有单位；
    程序表（program）写 columns 与 max_rows：值是行的列表（充放电工步、升温程序） */
export type ParamSpec = {
  type?: 'number' | 'integer' | 'enum' | 'program'; unit?: string; required?: boolean; options?: string[];
  columns?: ProgramColumn[]; max_rows?: number | null;
};

/** 工位的能力极限：数值参数 [下限, 上限]，选项型参数是允许的选项，程序表按列写 {列: 极限}（没写的列不约束） */
export type LimitWindow = [number, number] | string[] | Record<string, [number, number] | string[]>;

export type CapabilityRow = {
  id: string;
  name: string;
  params: Record<string, string>;
  param_specs?: Record<string, ParamSpec>;
  recovery: Recovery;
  stations: string[];
  retired: boolean;
  recipes: string[];
  delete_blockers: string[];
};

/** 批号。数量一律是十进制字符串——前端只显示，不做浮点运算。 */
export type LotRow = {
  id: string;
  material: string;
  material_id: string;
  material_code: string;
  base_unit: string;
  cas: string;
  type: string;
  /** 账面库存：组织仍持有且未消耗的总量，含已领用未消耗部分 */
  qty: string;
  /** 未耗用占用 = 授权预留 − 已消耗 − 已核销损耗 − 已释放 */
  reserved: string;
  outstanding: string;
  /** 已领用但未消耗、未归还的量；终止时它不能直接变回可用库存 */
  issued_outstanding: string;
  /** 可用量 = 账面库存 − 全部未耗用占用 */
  available: string;
  opening_balance: string;
  unit: string;
  release: string;
  sds: string;
  compat: string;
  expiry: string;
  opened: string;
  open_expiry: string;
  open_expiry_basis: string;
  /** 有效截止 = 生产有效期与开封有效期中的较早者 */
  effective_expiry: string;
  effective_expiry_basis: string;
  expired: boolean;
  expiring_soon: boolean;
  storage: string;
  ghs: string[];
  state: string;
  scrap_reason: string;
  usable: boolean;
  use_blockers: string[];
  editable_fields: string[];
  delete_blockers: string[];
};

export type WasteRow = {
  id: string;
  kind: string;
  level_pct: number;
  capacity_l: number;
  over_threshold: boolean;
  delete_blockers: string[];
};

export type AlarmRow = {
  id: string;
  severity: number;
  severity_label: string;
  state: string;
  state_label: string;
  condition_active: boolean;
  owner: string;
  source_type: string;
  source_id: string;
  message: string;
  response: string;
  shelved_until: string;
  raised_at: string;
  /** device：设备侧条件，只能设备上报恢复；system：软件判定，可由人写明原因签名清除 */
  origin: 'device' | 'system';
  condition_key: string;
  /** 异常类别（设备故障 / 通信异常 / 超时 ……），按去重键与文案归类 */
  category: string;
  category_label: string;
};

export type AuditRow = {
  id?: number;
  time: string;
  user: string;
  role?: string;
  action: string;
  target?: string;
  sign: boolean;
  meaning: string;
  before: string;
  after: string;
  detail: string;
  signature_id?: string;
  command_id?: string;
  checkpoint_id?: string;
};

export type ServiceIdentityRow = {
  id: string;
  source: string;
  name: string;
  state: 'active' | 'disabled';
  scopes: {
    stations?: string[];
    analysis_tasks?: 'all' | string[];
    instrument_serials?: string[];
    plan_proposals?: 'all' | string[];
    batch_signals?: 'all' | string[];
    environment_zones?: 'all' | string[];
    /** 上游系统可以向哪些配液模板提交配方表（POST /runtime/formulation-templates/{编号}/imports） */
    formulation_imports?: 'all' | string[];
  };
  created_at: string;
  rotated_at: string | null;
  last_used_at: string | null;
  row_version: number;
};

export type AccessLogRow = {
  id: number;
  time: string;
  org_id: string;
  subject: string;
  subject_kind: string;
  method: string;
  path: string;
  outcome: string;
  code: string;
  reason: string;
  request_id: string;
};

export type SchemaState = {
  current: string | null;
  expected: string;
  compatible: boolean;
};

export type RoleKey =
  | 'researcher' | 'qa' | 'operator' | 'ehs' | 'automation_engineer' | 'lab_manager' | 'auditor' | 'admin';

/** 组织的角色权限矩阵。系统管理员恒有全部权限，不在 matrix 里。 */
export type RolePermissions = {
  catalog: { group: string; permissions: { key: string; label: string }[] }[];
  roles: { key: RoleKey; name: string; locked: boolean }[];
  matrix: Record<string, string[]>;
  defaults: Record<string, string[]>;
  customized: boolean;
  row_version: number;
  updated_by: string;
  updated_at: string | null;
};

export type AccountRow = {
  id: string;
  username: string;
  display_name: string;
  role: RoleKey;
  roles: RoleKey[];
  role_name: string;
  account_state: 'active' | 'disabled';
  membership_state: 'active' | 'revoked';
  default_lab_id: string;
  must_change_password: boolean;
  password_changed_at: string | null;
  row_version: number;
};

export type ScheduleBoard = {
  now: string;
  stations: {
    id: string;
    name: string;
    island: number;
    status: string;
    channels?: number;
    channel_unit?: ChannelUnit;
    held: boolean;
    items: {
      batch_id: string;
      batch_state: string;
      step_index: number;
      step_name: string;
      kind: string;
      units?: number;
      hard: { maxGapMin?: number } | null;
      starts_at: string;
      ends_at: string;
      uncertain: boolean;
      forecast?: boolean;
      forecast_reason?: string;
    }[];
  }[];
  conflicts: { station_id: string; a: { batch_id: string }; b: { batch_id: string }; overlap_min: number }[];
};

export type QueueRow = {
  batch_id: string;
  recipe: string;
  version: string;
  priority: number;
  /** 任务的期望完成时间 */
  due_at: string | null;
  plan_id: string;
  material_ok: boolean;
  schedulable: boolean;
  blocker: string;
  makespan_min: number | null;
  path: { step_index: number; step_name: string; station_id: string; kind: string; starts_at: string; ends_at: string }[];
};

export type OptimizePreview = {
  /** 预览用的起点；应用时原样带回 */
  start_from: string;
  mode: 'optimize' | 'priority' | 'deadline' | 'fifo';
  /** 各批次任务的期望完成时间 */
  due: Record<string, string | null>;
  baseline: { ok: boolean; order: string[]; span_min?: number | null; weighted_min?: number | null; finish_at?: string; reason: string };
  best: {
    ok: boolean; order: string[]; span_min: number; weighted_min?: number; finish_at: string;
    tardiness_min?: number | null; lateness?: Record<string, number>;
  };
  improvement_min: number | null;
  evaluated: number;
  /** exhaustive：穷举全部顺序；local_search：迭代局部搜索 */
  method?: 'exhaustive' | 'local_search';
  elapsed_ms?: number;
  /** 装了 OR-Tools 时 CP-SAT 给出的候选；status 为 unavailable 表示未安装 */
  solver?: { status: string; order?: string[]; span_min?: number | null; gap_pct?: number | null; wall_ms?: number; note?: string; reason?: string } | null;
};

export type Dashboard = {
  now: string;
  gate: Gate;
  counts: Record<string, number>;
  todo: (NextAction & { batch_id: string; state: string })[];
  hard_windows: { batch_id: string; step_name: string; max_gap_min: number; deadline: string; remaining_min: number }[];
  alarms: AlarmRow[];
  active_batches: BatchSummary[];
  islands: { id: number; name: string; stations: number; running: number; fault: number; held: number }[];
  bottleneck: { station_id: string; name: string; busy_min: number } | null;
  recent_results: {
    batch_id: string;
    recipe_name: string;
    sample_done: number;
    sample_count: number;
    result_count: number;
    pending_review: number;
    official_count: number;
    is_golden: boolean;
  }[];
  plans: {
    id: string;
    name: string;
    plan_type: string;
    state: string;
    approval_state: string;
    batches: string[];
    sample_count: number;
  }[];
  organization_id: string;
  /** 我的待办：按用户与组织范围算，组织外的任务不会混进计数 */
  my_tasks: TaskRow[];
  pending_accept: TaskRow[];
  manual_todos: StepRunRow[];
  review_todos: StepRunRow[];
  pending_result_reviews: {
    id: string;
    analysis_task_id: string;
    metric_definition_id: string;
    result_version: number;
    created_at: string;
  }[];
  report_reviews: ReportVersionRow[];
  resources: {
    expiring_qualifications: QualificationRow[];
    unavailable_assets: AssetRow[];
  };
  materials: {
    expiring: {
      lot_id: string;
      material: string;
      expiry: string;
      open_expiry: string;
      effective_expiry: string;
      basis: string;
      expired: boolean;
      release: string;
    }[];
    waste: WasteRow[];
  };
  role: string;
};

export type TelemetrySeries = {
  station_id: string;
  metric: string;
  label: string;
  capability: string;
  setpoint: number | null;
  points: { t: string; v: number | null; setpoint: number | null; quality: string }[];
  golden: (number | null)[];
};

export type TelemetryFeed = {
  batch_id: string;
  state: string;
  golden_batch_id: string;
  series: TelemetrySeries[];
};

/* ---------- 组织与访问范围 ---------- */

export type Organization = { id: string; code: string; name: string; timezone: string };

export type Paged<T> = { items: T[]; total: number; page: number; page_size: number };

/* ---------- 人员与资质 ---------- */

export type QualificationRow = {
  id: string;
  person_id: string;
  scope_kind: 'capability' | 'sop' | 'safety';
  scope_ref: string;
  label: string;
  evidence_file_id: string;
  granted_by: string;
  effective_from: string;
  expires_at: string | null;
  revoked_at: string | null;
  revoke_reason: string;
  status: 'valid' | 'expiring' | 'expired' | 'revoked' | 'pending';
  valid_now: boolean;
  person_name?: string;
  person_code?: string;
};

export type MemberRow = {
  user_id: string;
  username: string;
  display_name: string;
  role: string;
  active: boolean;
  bound_person_id: string;
  bound_person: string;
};

export type PersonRow = {
  id: string;
  code: string;
  name: string;
  lab_id: string;
  title: string;
  contact: string;
  employment_state: string;
  employment_label: string;
  user_id: string;
  username: string;
  account_active: boolean;
  note: string;
  row_version: number;
  qualification_count: number;
  valid_qualification_count: number;
  expiring_count: number;
  employable: boolean;
  qualifications?: QualificationRow[];
  audit?: AuditRow[];
};

/* ---------- 资产、校准与预约 ---------- */

export type CalibrationRow = {
  id: string;
  asset_id: string;
  capability_scope: string[];
  result: 'pass' | 'fail';
  effective_from: string;
  expires_at: string | null;
  certificate_file_id: string;
  registered_by: string;
  note: string;
  valid_now: boolean;
};

export type BookingRow = {
  id: string;
  asset_id: string;
  asset_name: string;
  station_id: string;
  kind: string;
  kind_label: string;
  starts_at: string;
  ends_at: string;
  reason: string;
  state: string;
  batch_id: string;
  created_by: string;
  row_version: number;
};

export type AssetRow = {
  id: string;
  asset_no: string;
  name: string;
  model: string;
  vendor: string;
  serial: string;
  firmware: string;
  lab_id: string;
  location: string;
  state: string;
  capacity: number;
  calibration_applicable: boolean;
  calibration_exempt_reason: string;
  owner_person_id: string;
  owner_name: string;
  note: string;
  row_version: number;
  station_ids: string[];
  calibration_valid: boolean;
  calibration_due: string | null;
  unavailable_reasons: string[];
  calibrations?: CalibrationRow[];
  bookings?: BookingRow[];
  audit?: AuditRow[];
};

/* ---------- 样本 ---------- */

export type TransferRow = {
  id: string;
  physical_sample_id: string;
  kind: string;
  kind_label: string;
  from_location: string;
  to_location: string;
  from_party: string;
  to_party: string;
  quantity: string | null;
  unit: string;
  confirm_method: string;
  note: string;
  created_by: string;
  occurred_at: string;
};

export type SampleLocation = {
  kind: 'labware' | 'location' | 'text' | 'none';
  labware: { id: string; barcode: string; type_name: string; state: string } | null;
  well: string;
  place: { id: string; name: string; kind: string; station_id: string } | null;
  text: string;
};

export type SampleRow = {
  id: string;
  barcode: string;
  project_id: string;
  source: string;
  sample_type: string;
  parent_id: string | null;
  quantity: string | null;
  unit: string;
  storage_condition: string;
  current_location: string;
  /** 结构化位置：在哪块载具的哪个孔位（载具在哪），或放在哪个登记位置；都没有时只有文本 */
  location?: SampleLocation;
  location_note: string;
  custodian: string;
  lifecycle_state: string;
  lifecycle_label: string;
  note: string;
  origin: string;
  created_at: string;
  row_version: number;
  child_count: number;
  assignment_count: number;
};

export type SampleDetail = SampleRow & {
  lineage: SampleRow[];
  /** 合并出来的样本：全部母样（谱系链只沿第一个母样往上走） */
  parents?: SampleRow[];
  children: SampleRow[];
  assignments: {
    id: string;
    batch_id: string;
    container_id: string;
    well: string;
    condition_group: string;
    condition_label: string;
    repeat: number;
    state: string;
  }[];
  transfers: TransferRow[];
  slots: { container_id: string; well: string; occupied_at: string; released_at: string | null }[];
  analysis_tasks: {
    id: string;
    round_no: number;
    state: string;
    method: string;
    method_version: string;
    required_metrics: string[];
    retest_of: string;
    created_at: string;
  }[];
  attachments: { id: string; filename: string; media_type: string; state: string }[];
  audit: AuditRow[];
};

/* ---------- 实验任务 ---------- */

export type TaskRow = {
  id: string;
  title: string;
  plan_id: string;
  plan_name: string;
  plan_type: string;
  plan_version: number;
  owner_user_id: string;
  owner_name: string;
  assignee_user_id: string;
  assignee_name: string;
  reviewer_user_id: string;
  reviewer_name: string;
  batch_id: string;
  batch_state: string;
  sample_ids: string[];
  due_at: string | null;
  overdue: boolean;
  priority: number;
  state: string;
  state_label: string;
  stored_state: string;
  accepted_at: string | null;
  cancel_reason: string;
  note: string;
  created_at: string;
  row_version: number;
  /** 任务树：父任务编号，顶层为空 */
  parent_id: string;
  /** 上游任务：满足放行条件后本任务才能下发 */
  depends_on: string[];
  /** 从父任务继承来的上游（拆分出来的子任务同样要等它们） */
  inherited_depends_on: string[];
  /** 上游怎样才算满足：运行结束 / 数据复核通过 / 报告发布放行 */
  dependency_gate: DependencyGate;
  dependency_gate_label: string;
  /** 方案当前的最新批准版本；高于 plan_version 时可以显式迁移 */
  latest_plan_version: number | null;
  children: TaskChild[];
  /** 还没满足的上游 */
  blocked_by: { task_id: string; label: string }[];
  /** 子任务在父任务里负责的那一份：按数量拆的 count、矩阵按重复拆的 repeats、补测的 groups、全局序号偏移 offset、
   *  整体重复的第几次 replica。空对象表示按样本清单或方案整体执行 */
  portion: TaskPortion;
  portion_label: string;
  /** 计划样本数：叶子任务是本份，父任务是全部子任务的计划合计（不含补测） */
  planned_count: number;
  /** 父任务怎么拆的：parallel 并行 / pilot 首批验证后放行其余 / sequential 逐批顺序 */
  split_mode: '' | SplitMode;
  split_mode_label: string;
  /** retest：补测子任务 */
  purpose: '' | 'retest';
  /** 按样本算的进度；父任务总是有，叶子任务只在详情里有 */
  progress: TaskProgress | null;
  /** 「按现有结果结束、不再补测」的签名记录 */
  shortfall_decisions: ShortfallDecision[];
};

export type SplitMode = 'parallel' | 'pilot' | 'sequential';

export const SPLIT_MODE_HINT: Record<SplitMode, string> = {
  parallel: '各批独立排程：同一台设备一次跑一批由排程按通道数错开，能流水线的就流水线',
  pilot: '先做第一批，数据复核通过后其余几批才能开跑；首批不合格就不浪费后面的样本',
  sequential: '每批等上一批整个流程跑完才开工，不能流水线；只在工艺上确实有先后时用',
};

export type TaskPortion = {
  count?: number;
  repeats?: number;
  groups?: Record<string, number>;
  offset?: number;
  replica?: number;
};

export type TaskChild = {
  id: string;
  title: string;
  batch_id: string;
  batch_state: string;
  state: string;
  state_label: string;
  purpose: '' | 'retest';
  planned_count: number;
  portion: TaskPortion;
  portion_label: string;
  depends_on: string[];
  dependency_gate_label: string;
  valid: number;
  failed: number;
  running: number;
  pending: number;
};

/** 按样本算的进度。短缺按条件组算：一个条件缺的样本不能拿另一个条件多做的补上 */
export type TaskProgress = {
  /** 计划（不含补测、不含没下发就取消的子任务） */
  target: number;
  valid: number;
  failed: number;
  running: number;
  pending: number;
  /** 没下发就取消的子任务：不做了，不算短缺 */
  descoped: number;
  /** 补测子任务的计划量 */
  retest: number;
  /** 已签名放弃的个数 */
  accepted: number;
  shortfall: number;
  groups: Record<string, { target: number; valid: number; open: number; failed: number }>;
};

export type ShortfallDecision = {
  count: number;
  reason: string;
  user_id: string;
  user: string;
  at: string;
  signature_id: string;
  valid: number;
  target: number;
};

export type SampleMapRow = {
  task_id: string;
  title: string;
  purpose: '' | 'retest';
  label: string;
  planned_count: number;
  batch_id: string;
  batch_state: string;
  samples: {
    id: string;
    physical_sample_id: string;
    well: string;
    condition_group: string;
    repeat: number;
    state: string;
  }[];
};

export type SplitPart = {
  index: number;
  size: number;
  portion: TaskPortion;
  sample_ids: string[];
  label: string;
};

export type SplitPreview = {
  total: number;
  capacity: number;
  needs_split: boolean;
  plan_type: string;
  modes: { key: SplitMode; label: string }[];
  mode: SplitMode;
  mode_label?: string;
  replicate?: boolean;
  conditions?: number | null;
  parts: SplitPart[];
  detail: string;
  error: string;
};

export type DependencyGate = 'run_completed' | 'data_validated' | 'released';

export const DEPENDENCY_GATE_LABEL: Record<DependencyGate, string> = {
  run_completed: '运行结束',
  data_validated: '数据复核通过',
  released: '报告发布放行',
};

export type DueWindow = {
  batch_id: string;
  step_index: number;
  step_name: string;
  max_gap_min: number;
  deadline: string;
  remaining_min: number;
};

export type TaskDetail = TaskRow & {
  history: {
    action: string;
    action_label: string;
    from_name: string;
    to_name: string;
    reason: string;
    actor_name: string;
    created_at: string;
  }[];
  step_runs: { id: string; step_index: number; kind: string; state: string; attempt: number }[];
  analysis_tasks: { id: string; state: string; round_no: number }[];
  report_id: string;
  audit: AuditRow[];
  /** 父任务：样本 → 子任务 → 批次 → 孔位 */
  sample_map?: SampleMapRow[];
};

/* ---------- 步骤运行 ---------- */

export type FormField = {
  key: string;
  label: string;
  type?: 'number' | 'text' | 'bool' | 'enum';
  required?: boolean;
  options?: (string | number)[];
  unit?: string;
  /** 按样本录入：每个在用样本各填一个数值（样本编号 → 数值），可作逐样本前馈的来源 */
  per_sample?: boolean;
};

/** 前馈参数：设定值 = 上游结果 × 系数（换算到参数单位），落在预期范围内才下发 */
export type ParamBinding = {
  source_step_id: string;
  field: string;
  scope: 'batch' | 'sample';
  /** 来源值的单位 */
  unit: string;
  /** 写在流程里的固定系数（随流程审批冻结），或引用方案因子（按样本的水平取值）；不写就是纯单位换算 */
  coefficient?: { value?: number | ''; unit?: string; factor?: string } | null;
  /** 计算结果的预期范围（参数单位）：排程按它匹配工位极限，下发时超出同样不下发 */
  expect: [number | '', number | ''];
};

/** 指令上的前馈求值记录：每个样本一条 */
export type BindingRecord = {
  param: string;
  label: string;
  scope: 'batch' | 'sample';
  sample_id: string;
  well: string;
  source_step_id: string;
  source_name: string;
  source_kind: 'device' | 'manual';
  source_ref: string;
  source_attempt: number;
  field: string;
  raw: string;
  unit: string;
  coefficient: string;
  coefficient_unit: string;
  coefficient_source: string;
  value: string;
  target_unit: string;
};

export type StepRunRow = {
  id: string;
  batch_id: string;
  step_id: string;
  step_index: number;
  step_name: string;
  kind: StepKindName;
  kind_label: string;
  attempt: number;
  state: string;
  state_label: string;
  station_id: string;
  assignee_user_id: string;
  assignee_name: string;
  due_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  form: FormField[];
  form_data: { values?: Record<string, unknown>; checks?: Record<string, boolean>; note?: string; [key: string]: unknown };
  /** 设备回报对照方法输出规则的打标（越界、缺必报项）；不阻断流程 */
  flags?: DataFlag[];
  requires_signature: boolean;
  review_role: string;
  wait_for: { mode?: string; event?: string };
  branch: BranchConfig;
  branch_cases: { key: string; label: string; loop: boolean }[];
  skippable: boolean;
  timeout: StepTimeout | null;
  deadline_at: string | null;
  timed_out_at: string | null;
  groups: SubflowGroup[];
  conclusion: string;
  reason: string;
  submitted_by: string;
  submitted_by_name: string;
  reviewed_by: string;
  reviewed_by_name: string;
  row_version: number;
};

export type WorkflowEventRow = {
  id: string;
  event_key: string;
  event_type: string;
  state: string;
  error: string;
  available_at: string;
  processed_at: string | null;
  payload: Record<string, unknown>;
};

/* ---------- 指标与检测 ---------- */

/** 从曲线派生数值：取法见后端 domain/series.py 的 REDUCERS */
export type SeriesReducer = 'last_y' | 'first_y' | 'max_y' | 'min_y' | 'last_x' | 'max_x' | 'area';

export type MetricRow = {
  id: string;
  code: string;
  name: string;
  version: string;
  /** series：一组 x–y 点（充放电曲线、谱图），单位是 y 的单位 */
  value_type: 'number' | 'text' | 'enum' | 'series';
  unit: string;
  method_version: string;
  sample_types: string[];
  rules: {
    min?: number; max?: number; options?: (string | number)[];
    x_label?: string; x_unit?: string; max_points?: number;
    derived?: { metric: string; of: SeriesReducer }[];
  };
  state: string;
  referenced_by: number;
  numeric: boolean;
  editable: boolean;
  created_at: string;
};

export type DataFlag = { code: string; message: string; rule_id?: string; key?: string; well?: string };

/** 一条曲线（或一个结果里的几条之一） */
export type CurveTrace = { name: string; x: number[]; y: number[] };

/** 曲线概要：条数、点数、x / y 范围 */
export type CurveSummary = {
  trace_count: number;
  points: number;
  x_range: [number, number] | null;
  y_range: [number, number] | null;
};

export type DataRuleRow = {
  id: string;
  name: string;
  left_metric: string;
  op: '<' | '<=' | '>' | '>=' | '==' | '!=';
  right_metric: string;
  right_value: number | null;
  factor: number;
  offset: number;
  severity: 'flag' | 'reject';
  severity_label: string;
  enabled: boolean;
  note: string;
  expression: string;
  row_version: number;
};

export type ResultValueRow = {
  id: string;
  analysis_task_id: string;
  physical_sample_id: string;
  assignment_id: string;
  metric_definition_id: string;
  metric_code: string;
  metric_name: string;
  value_type: string;
  value: number | string | null;
  display: string;
  /** 曲线：概要 + 几百点的缩略；完整的点用 /result-values/{id}/series 取 */
  series?: (CurveSummary & { preview: CurveTrace[]; x_label: string; x_unit: string }) | null;
  unit: string;
  collected_at: string | null;
  raw_file_id: string;
  source_ref: string;
  parser_version: string;
  result_version: number;
  revises_id: string;
  superseded_by_id: string;
  not_measured_reason: string;
  quality: string;
  quality_label: string;
  review_state: string;
  review_label: string;
  provenance: string;
  entered_by: string;
  entered_by_name: string;
  row_version: number;
  /** 自动打标：越界、逻辑冲突。不改变值，交审核下结论 */
  flags: DataFlag[];
  station_id: string;
  instrument: string;
  official: boolean;
  reviews: {
    id: string;
    reviewer_id: string;
    reviewer_name: string;
    conclusion: string;
    quality: string;
    reason: string;
    result_version: number;
    decided_at: string;
  }[];
};

export type AnalysisTaskRow = {
  id: string;
  sample_id: string;
  physical_sample_id: string;
  method: string;
  method_version: string;
  round_no: number;
  state: string;
  state_label: string;
  external_ref: string;
  retest_of: string;
  created_at: string;
  row_version: number;
  required_metrics: {
    id: string;
    code: string;
    name: string;
    unit: string;
    /** 录入控件按它给：number 以数值回传，enum 从 options 里选，series 按两列粘贴 */
    value_type?: 'number' | 'text' | 'enum' | 'series';
    options?: string[];
    x_label?: string;
    x_unit?: string;
    collected: boolean;
    not_measured: boolean;
  }[];
  collected: boolean;
  missing_metrics: string[];
  review_pending: number;
  values?: ResultValueRow[];
};

/* ---------- SOP ---------- */

export type SopStep = {
  /** 稳定标识：编辑时原样带回，新加的步骤留空由服务端生成 */
  key?: string;
  title: string;
  kind: 'device' | 'manual' | 'wait' | 'review';
  capability: string;
  params: Record<string, number>;
  duration_min: number;
  instructions: string;
  checks: string[];
};

/** 已发布版本按时间细分：生效中 / 待生效 / 已被取代 / 已失效；其余与 state 相同 */
export type SopStatus = 'draft' | 'review' | 'retired' | 'effective' | 'pending' | 'superseded' | 'expired';

export type SopSnapshot = {
  sop_version_id?: string;
  sop_id?: string;
  code?: string;
  title?: string;
  version?: string;
  file_id?: string;
  filename?: string;
  file_checksum?: string;
  capability_scope?: string[];
  sample_types?: string[];
  requires_training_ack?: boolean;
  steps?: SopStep[];
  effective_from?: string | null;
  frozen_at?: string;
  /** 流程关联的版本在开批时已被取代：记下原关联版本 */
  linked_version_id?: string;
  linked_version?: string;
};

export type SopImpact = {
  impacted_recipes: { id: string; name: string; version: string; state: string; sop_version?: string }[];
  impacted_batches: { id: string; state: string; sop_version: string }[];
};

export type SopVersionRow = {
  id: string;
  sop_id: string;
  code: string;
  title: string;
  version: string;
  state: string;
  state_label: string;
  status: SopStatus;
  status_label: string;
  effective: boolean;
  category: string;
  owner_id: string;
  owner_name: string;
  effective_to: string | null;
  review_due: string | null;
  review_overdue: boolean;
  superseded_by: string;
  superseded_by_version: string;
  file_id: string;
  filename: string;
  file_checksum: string;
  capability_scope: string[];
  sample_types: string[];
  requires_training_ack: boolean;
  effective_from: string | null;
  author_id: string;
  author_name: string;
  approver_id: string;
  approver_name: string;
  published_at: string | null;
  retired_at: string | null;
  reject_reason: string;
  row_version: number;
  editable: boolean;
  ack_count: number;
  /** 数字 SOP：结构化步骤 */
  steps: SopStep[];
  restored_from: string;
  acks?: { person_id: string; person_name: string; acked_at: string }[];
  using_recipes?: { id: string; name: string; version: string; state: string }[];
  active_batches?: { id: string; state: string; sop_version: string }[];
  audit?: AuditRow[];
};

/* ---------- 报告 ---------- */

export type ReportVersionRow = {
  id: string;
  report_id: string;
  code: string;
  title: string;
  task_id: string;
  batch_id: string;
  version: number;
  state: string;
  state_label: string;
  template_version: string;
  algorithm_version: string;
  author_id: string;
  author_name: string;
  approver_id: string;
  approver_name: string;
  pdf_file_id: string;
  supersedes_id: string;
  reject_reason: string;
  submitted_at: string | null;
  approved_at: string | null;
  published_at: string | null;
  created_at: string;
  row_version: number;
  readonly: boolean;
  content?: ReportContent;
  publish_snapshot?: Record<string, unknown>;
  audit?: AuditRow[];
};

export type ReportContent = {
  title: string;
  header: Record<string, string | number>;
  plan_section: Record<string, string>;
  method_section: Record<string, string>;
  samples: Record<string, string>[];
  resources: {
    owner: string;
    assignee: string;
    reviewer: string;
    stations: string[];
    materials: Record<string, string>[];
  };
  execution: { step_index: number; kind_label: string; step_name: string; state_label: string; note: string }[];
  exceptions: string[];
  results: { metric_name: string; unit: string; rows: Record<string, unknown>[] }[];
  exclusions: Record<string, string | number>[];
  statistics: Record<string, unknown>[];
  /** 曲线型指标的正式结果：每个指标一张按样本叠加的图（点已抽稀，条数封顶） */
  curves?: {
    metric_name: string;
    unit: string;
    x_label: string;
    x_unit: string;
    total: number;
    shown: number;
    excluded: number;
    traces: { label: string; group: string; x: number[]; y: number[] }[];
  }[];
  conclusion: string;
  batch_id: string;
  /** 父任务的多批合并报告：引用的全部批次、父任务，以及「分批情况」一节 */
  batch_ids?: string[];
  task_id?: string;
  batches?: {
    rows: NonNullable<AnalysisView['batches']>;
    progress: Omit<TaskProgress, 'groups'>;
    decisions: ShortfallDecision[];
    metrics: {
      metric_name: string;
      unit: string;
      comparable: boolean;
      comparable_reason: string;
      by_batch: { batch_id: string; n_included: number; n_excluded: number; mean: string; sd: string; cv_pct: string }[];
      batch_effect: (Omit<BatchEffect, 'f' | 'p'> & { f: string; p: string }) | null;
    }[];
  };
  /** 以下章节在报告模板 2.0 起提供；老报告没有 */
  instruments?: ReportInstrument[];
  operation_log?: { time: string; user: string; action: string; before: string; after: string; detail: string; signed: boolean; meaning: string }[];
  raw_files?: { id: string; filename: string; media_type: string; size: number; checksum: string; usage: string[] }[];
  data_flags?: { scope: string; target: string; code: string; message: string; quality: string; review_state: string }[];
  /** 生成时的模板快照：章节键顺序；组织模板另带改过的标题与固定文字章节 */
  template?: {
    key: string; name: string; version: string; sections: string[];
    titles?: Record<string, string>; texts?: Record<string, { title: string; body: string }>;
  };
};

export type ReportInstrument = {
  station_id: string;
  name: string;
  model: string;
  asset_no: string;
  vendor: string;
  serial: string;
  firmware: string;
  driver: string;
  kind: string;
  calibration: string;
  methods: string[];
  steps: string[];
};

export type ReportTemplate = {
  key: string;
  name: string;
  version: string;
  description: string;
  sections: { key: string; title: string }[];
  /** 内置模板（代码里，只读）还是组织自己的模板 */
  builtin?: boolean;
};

/** 组织报告模板的一节：内置章节（可改标题）或固定文字章节（声明、方法说明） */
export type ReportTemplateSection = { key: string; title?: string; kind?: 'text'; body?: string };

export type CustomReportTemplate = {
  id: string;
  key: string;
  version: number;
  name: string;
  description: string;
  /** draft | released | retired */
  state: string;
  state_label: string;
  builtin: false;
  sections: ReportTemplateSection[];
  /** 起草人的用户 id：起草人不能发布本人起草的模板 */
  created_by: string;
  created_by_name: string;
  released_by_name: string;
  created_at: string | null;
  released_at: string | null;
  row_version: number;
};

export type ReportTemplateListing = {
  builtin: ReportTemplate[];
  custom: CustomReportTemplate[];
  /** 可选的内置章节 */
  sections: { key: string; title: string }[];
};

/* ---------- 正式统计 ---------- */

export type DatasetGroup = {
  group: string;
  label: string;
  is_control: boolean;
  n_included: number;
  n_excluded: number;
  n_total: number;
  mean: number | null;
  sd: number | null;
  cv_pct: number | null;
  unit: string;
  observations: {
    assignment_id: string;
    analysis_task_id: string;
    round_no: number;
    result_version: number;
    repeat: number;
    value: number | null;
    quality: string;
    review_state: string;
  }[];
  excluded: {
    assignment_id: string;
    analysis_task_id: string;
    result_version: number;
    reason: string;
    reason_label: string;
    quality: string;
    review_state: string;
  }[];
};

export type MetricBlock = {
  metric_id: string;
  metric_name: string;
  unit: string;
  groups: DatasetGroup[];
  effects: { factor: string; unit: string; levels: { level: unknown; mean: number | null; n: number }[]; range: number | null }[];
  summary: {
    metric_id: string;
    metric_name: string;
    unit: string;
    official: boolean;
    groups: number;
    included: number;
    excluded: number;
    exclusions: { reason: string; label: string; count: number }[];
    mean: number | null;
    sd: number | null;
    cv_pct: number | null;
    median_cv_pct: number | null;
    high_cv_groups: number;
    single_repeat: boolean;
    /** 均值最高与最低的条件组：只按高低列出，不判优劣（只有一组时没有最低） */
    highest_group: string | null;
    highest_mean: number | null;
    lowest_group: string | null;
    lowest_mean: number | null;
  };
  excluded: DatasetGroup['excluded'];
  /** 以下只在父任务的合并视图里有：各批单位、方法版本是否一致，分批明细与批次差异 */
  comparable?: boolean;
  comparable_reason?: string;
  by_batch?: BatchStat[];
  batch_effect?: BatchEffect | null;
};

export type BatchStat = {
  batch_id: string;
  n_included: number;
  n_excluded: number;
  mean: number | null;
  sd: number | null;
  cv_pct: number | null;
};

/** 批次之间有没有系统差异：单因素方差分析，矩阵方案先按条件组校正 */
export type BatchEffect = {
  method: 'anova' | 'anova_centered';
  f: number | null;
  df1: number;
  df2: number;
  p: number | null;
  alpha?: number;
  significant: boolean;
  note: string;
};

export type AnalysisView = {
  /** 父任务的合并视图：任务、各批次与按样本的进度；单批视图没有这些 */
  task_id?: string;
  task_title?: string;
  batch_ids?: string[];
  batches?: {
    batch_id: string;
    task_id: string;
    title: string;
    state: string;
    state_label?: string;
    purpose: string;
    portion_label: string;
    samples: number;
    recipe_version: string;
  }[];
  progress?: TaskProgress;
  batch_id: string;
  plan_id: string;
  plan_name: string;
  plan_type: string;
  recipe_id: string;
  recipe_name: string;
  state: string;
  official: boolean;
  scope_label: string;
  available_metrics: { id: string; code: string; name: string; unit: string; numeric: boolean; value_type?: string }[];
  selected_metrics: string[];
  metrics: MetricBlock[];
  non_numeric_metrics: { metric_id: string; metric_name: string; value_type: string }[];
  /** 曲线指标：不进数值统计，按样本叠加画（/results/{batch}/series） */
  series_metrics?: { metric_id: string; metric_name: string; unit: string; x_label: string; x_unit: string }[];
  show_factor_effects: boolean;
};

/** 曲线叠加：一个曲线指标在批次（或父任务各批次）里每个样本的当前曲线，纳入口径与数值统计相同 */
export type SeriesView = {
  batch_id?: string;
  task_id?: string;
  metric_id: string;
  metric_code: string;
  metric_name: string;
  unit: string;
  x_label: string;
  x_unit: string;
  official: boolean;
  scope_label: string;
  samples: (CurveSummary & {
    assignment_id: string;
    batch_id: string;
    analysis_task_id: string;
    result_value_id: string;
    result_version: number;
    condition_group: string;
    condition_label: string;
    is_control: boolean;
    well: string;
    quality: string;
    review_state: string;
    traces: CurveTrace[];
  })[];
  excluded: {
    assignment_id: string;
    condition_label: string;
    result_version: number;
    reason: string;
    reason_label: string;
  }[];
};

/** 一条曲线结果的完整数据点 */
export type SeriesDetail = CurveSummary & {
  id: string;
  metric_code: string;
  metric_name: string;
  result_version: number;
  unit: string;
  x_label: string;
  x_unit: string;
  traces: CurveTrace[];
};

/* ---------- 库存 ---------- */

export type LedgerLine = {
  id: string;
  source: string;
  event_id: string;
  line_no: number;
  event_type: string;
  event_label: string;
  lot_id: string;
  reservation_id: number | null;
  batch_id: string;
  step_run_id: string;
  quantity: string;
  unit: string;
  balance_delta: string;
  balance_after: string;
  operator: string;
  note: string;
  created_at: string;
};

export type LotLedger = {
  lot_id: string;
  material: string;
  unit: string;
  opening_balance: string;
  balance: string;
  outstanding: string;
  issued_outstanding: string;
  available: string;
  ledger_sum: string;
  reconciled: boolean;
  lines: LedgerLine[];
};

export type MaintenanceOrderRow = {
  id: string;
  asset_id: string;
  asset_label: string;
  kind: 'preventive' | 'corrective' | 'inspection';
  kind_label: string;
  title: string;
  detail: string;
  planned_start: string;
  planned_end: string;
  state: 'planned' | 'in_progress' | 'done' | 'cancelled';
  state_label: string;
  booking_id: string;
  started_at: string | null;
  completed_at: string | null;
  result: '' | 'pass' | 'fail';
  record: string;
  row_version: number;
};

export type DesignSpace = {
  /** 数值因子写上下限；类别因子（溶剂、催化剂、协议）写允许的选项 */
  bounds?: Record<string, { min?: number | null; max?: number | null; options?: (number | string)[] }>;
  forbidden?: Record<string, number | string>[];
  max_points?: number;
};

export type ProposalRow = {
  id: string;
  plan_id: string;
  proposal_id: string;
  source: string;
  model_version: string;
  rationale: string;
  points: Record<string, number | string>[];
  state: 'accepted' | 'rejected';
  issues: string[];
  created_plan_id: string;
  /** 由哪次分析运行生成（运行记着输入快照、程序与模型版本、参数与随机种子） */
  analysis_run_id?: string;
  created_at: string;
};

/** 训练数据快照：固化纳入的结果版本、排除清单与数据行，之后按快照导出不变 */
export type DatasetSnapshotRow = {
  id: string;
  plan_id: string;
  root_plan_id: string;
  key: string;
  digest: string;
  row_count: number;
  excluded_count: number;
  file_count: number;
  note: string;
  created_by: string;
  created_at: string;
  /** 快照里后来被更正、退回或改判的结果数 */
  changed_count?: number;
};

export type AnalysisRunRow = {
  id: string;
  plan_id: string;
  run_id: string;
  snapshot_id: string;
  program: string;
  program_version: string;
  model_version: string;
  params: Record<string, unknown>;
  seed: string;
  outputs: Record<string, unknown>;
  created_by: string;
  created_at: string;
};

/* ---------- 载具、位置与现场总览 ---------- */

export type LabwareBrief = { id: string; barcode: string; batch_id: string; state: string };

export type FloorSlot = {
  id: string;
  name: string;
  kind: string;
  active: boolean;
  position: number;
  labware: LabwareBrief | null;
  incoming: { command_id: string; state: string; barcode: string } | null;
};

export type FloorCommand = {
  id: string;
  type: string;
  state: string;
  batch_id: string;
  /** 本组织的指令才能在这里转人工核查 */
  mine: boolean;
  error: string;
  step_index: number;
  motion: boolean;
  since: string;
};

export type FloorStation = {
  id: string;
  name: string;
  island: number;
  status: string;
  retired: boolean;
  channels: number;
  capabilities: string[];
  /** 只做转运的承运工位（AGV），不涉及清洗 */
  carrier: boolean;
  /** 本组织的工位：清洗确认、重连只对它可用 */
  mine: boolean;
  clean: boolean;
  dirty_batch_id: string;
  row_version: number;
  adapter: {
    connected: boolean;
    enabled: boolean;
    interlock: boolean;
    accepts_commands: boolean;
    kind: string;
    /** 与执行门同一口径：online / degraded / stale 心跳超时 / acceptance 待接入验收 / offline 失联 / disabled */
    status: string;
    heartbeat_age_sec: number | null;
    current_command_id: string;
    /** 配置变更后还欠的接入验收 */
    acceptance?: { required: string; reason: string; required_label: string };
  } | null;
  commands: FloorCommand[];
  nests: FloorSlot[];
};

export type FloorTransfer = {
  id: string;
  state: string;
  carrier: string;
  batch_id: string;
  barcode: string;
  from: string;
  to: string;
  since: string;
};

export type Floor = {
  now: string;
  tracking: boolean;
  stations: FloorStation[];
  storage: { group: string; slots: FloorSlot[] }[];
  transfers: FloorTransfer[];
  lost: { id: string; barcode: string; batch_id: string }[];
};

export type LabwareRow = {
  id: string;
  barcode: string;
  type_id: string;
  type_name: string;
  rows: number;
  cols: number;
  batch_id: string;
  /** 在批次里的角色：空为主载具 */
  role?: string;
  batch_active: boolean;
  location_id: string;
  state: string;
  in_transit: { command_id: string; state: string; carrier: string; to: string } | null;
  row_version: number;
};

export type LabwareType = { id: string; name: string; kind: string; rows: number; cols: number; active: boolean };

export type LocationRow = {
  id: string;
  name: string;
  kind: string;
  station_id: string;
  group: string;
  position: number;
  accepts: string[];
  active: boolean;
  occupant: string;
};


/** 统一异常事件：类别、影响面、自动处理与结果、人工处理与最终结果。 */
export type ExceptionEventRow = {
  id: string;
  category: string;
  category_label: string;
  severity: number;
  source_type: string;
  source_id: string;
  batch_id: string;
  step_id: string;
  step_index: number;
  station_id: string;
  command_id: string;
  alarm_id: string;
  message: string;
  impact: { batches?: string[]; samples?: number; stations?: string[]; tasks?: string[] };
  never_sent: boolean;
  state: string;
  state_label: string;
  rule_id: string;
  decision: string;
  auto_action: string;
  auto_action_label: string;
  auto_result: string;
  manual_action: string;
  manual_note: string;
  manual_by: string;
  final_result: string;
  created_at: string;
  updated_at: string | null;
  resolved_at: string | null;
};

export type ExceptionSummary = {
  open: number;
  auto_resolved: number;
  total: number;
  by_category: { category: string; label: string; count: number }[];
};

export type ExceptionRuleRow = {
  id: string;
  name: string;
  category: string;
  category_label: string;
  match: Record<string, string>;
  action: 'retry' | 'reroute' | 'skip' | 'reschedule' | 'hold';
  action_label: string;
  params: { max_attempts?: number; delay_sec?: number };
  priority: number;
  enabled: boolean;
  note: string;
  updated_at: string | null;
  row_version: number;
};

/** 重排建议：调度确认后才写入时间线。 */
export type ScheduleProposalRow = {
  id: string;
  trigger: string;
  trigger_label: string;
  reason: string;
  station_id: string;
  state: string;
  state_label: string;
  batch_ids: string[];
  after: Record<string, { from_step: number; allocations: { step_index: number; station_id: string; kind: string; starts_at: string; ends_at: string }[] }>;
  impact: Record<string, { state: string; from_step: number; old_end: string | null; new_end: string | null; delay_min: number | null; moved: string[]; late_min: number }>;
  unplanned: { batch_id: string; reason: string }[];
  auto_applied: boolean;
  created_at: string | null;
  decided_by: string;
  decided_at: string | null;
  note: string;
};


/** 运行驾驶舱指标（口径见 api/app/domain/kpi.py）。 */
export type KpiReport = {
  window: { start: string; end: string; hours: number };
  experiments: { running: number; queued: number; paused: number; exception: number; completed: number; tasks_created: number };
  automation: { completed: number; without_intervention: number; success_rate: number | null };
  exceptions: { raised: number; auto_resolved: number; open: number; mttr_min: number | null };
  utilization: {
    overall: number | null;
    stations: { station_id: string; name: string; channels: number; utilization: number; planned_load: number; busy_min: number }[];
  };
};

export type WebhookRow = {
  id: string;
  name: string;
  url: string;
  topics: string[];
  enabled: boolean;
  created_at: string | null;
  last_success_at: string | null;
  last_error: string;
  consecutive_failures: number;
  row_version: number;
  /** webhook 签名 JSON；wecom / dingtalk 群机器人；email 邮件 */
  channel: 'webhook' | 'wecom' | 'dingtalk' | 'email';
  channel_label: string;
  config: { to?: string[]; max_severity?: number };
  /** 钉钉机器人是否配置了加签密钥 */
  bot_signed: boolean;
  /** 只在新建 / 轮换的响应里出现一次 */
  secret?: string;
};

export type WebhookDeliveryRow = {
  id: string;
  subscription_id: string;
  event_id: string;
  topic: string;
  payload: Record<string, unknown>;
  state: string;
  attempts: number;
  next_attempt_at: string | null;
  last_error: string;
  response_status: number;
  created_at: string | null;
  delivered_at: string | null;
};

/* ---------- 配方导入（配液模板） ---------- */

/** 模板里的固定步骤：普通流程步骤，另带模板内唯一的 key 与引用其他固定步骤的 after */
export type FormulationFixedStep = RecipeStep & { key: string; after?: string[] };

export type FormulationStage = {
  key: string;
  label: string;
  /** 本阶段第一个加料步骤还要等哪些固定步骤 */
  after?: string[];
  /** 本阶段最后一个加料之后是否紧跟搅拌；缺省是 */
  stir_after_last?: boolean;
  /** false：不接上一个阶段的尾巴，只接 after；没汇合的尾巴由后面的阶段或后段一起接上 */
  chain?: boolean;
  /** 加料顺序：table 按表格列顺序（缺省），routes 按 routes 里类别的先后 */
  order?: 'table' | 'routes';
  /** 本阶段的加料后步骤，覆盖全局 stir */
  stir?: RecipeStep;
  then?: FormulationFixedStep[];
};

/** 物料类别 → 怎么加：进哪个阶段、用量写到哪个参数、加完是否紧跟搅拌、加料步骤长什么样。
    整任务方式（config.task）下只写 stage 与 not_last：怎么加由上位机定 */
export type FormulationRoute = {
  stage: string;
  param?: string;
  /** 不是阶段最后一个加料时是否紧跟搅拌；缺省是 */
  stir_after?: boolean;
  /** 这类料不能是一瓶在本阶段加的最后一种；写文字就是原因（如 EC 常温是固体） */
  not_last?: boolean | string;
  step?: RecipeStep;
  /** 这类料的加料后步骤，覆盖阶段与全局的 stir */
  stir?: RecipeStep;
};

/** 逐瓶参数列：表格里用量以外的一列（终混温度、每瓶分装量），每瓶的值作用于某个固定设备步骤的参数 */
export type FormulationRowParam = {
  key: string;
  /** 表格里这一列的列名（表头里的单位后缀照认） */
  header: string;
  label?: string;
  step: string;
  param: string;
  unit?: string;
  /** 空白格或表格没有这一列时用它；不给就每瓶都要写 */
  default?: number | string | null;
};

/** 分装量核对：每瓶总质量 ÷ density（g/mL）估算母液体积，分装瓶数 × 每瓶分装量 + 母瓶留样 reserve（mL）要放得下 */
export type FormulationVolumeCheck = {
  /** 实验参数或逐瓶参数的 key */
  bottles: string;
  volume: string;
  /** 物料主数据没登记密度（1 mL = x g）时用的缺省密度，g/mL */
  density?: number;
  reserve?: number;
};

export type FormulationExperimentParam = {
  key: string;
  label: string;
  /** 作用的固定步骤 key 与其能力参数 */
  step: string;
  param: string;
  unit: string;
  /** 数值，或选项型参数的选项 */
  default: number | string;
};

export type FormulationTemplateConfig = {
  plate: number;
  risk?: string;
  design?: string;
  unit?: string;
  sample_type?: string;
  serial_headers?: string[];
  required_metrics?: string[];
  prefix?: FormulationFixedStep[];
  stages?: FormulationStage[];
  routes?: Record<string, FormulationRoute>;
  stir?: RecipeStep;
  suffix?: FormulationFixedStep[];
  /** 整任务方式：上位机收整份实验任务时，固定设备步骤 step 一步投完一瓶的全部组分，表格里的料按加料顺序依次占用
      slots 列出的能力参数（加料位）。写了它，阶段与类别只排加料顺序，不再逐种料生成加料、搅拌步骤 */
  task?: { step: string; slots: string[] };
  experiment_params?: FormulationExperimentParam[];
  row_params?: FormulationRowParam[];
  volume_check?: FormulationVolumeCheck;
  /** 生成的流程按哪份 SOP 执行；步骤模板的 sop_step 写这份 SOP 里的步骤标题 */
  sop?: { code: string };
};

/** 实验参数 / 逐瓶参数作用的能力参数的规格：选项型的给下拉 */
export type FormulationParamSpec = { type: 'number' | 'integer' | 'enum' | 'program'; unit: string; options: string[]; label: string };

export type FormulationCheck = {
  ok: boolean;
  problems: string[];
  param_specs: Record<string, FormulationParamSpec>;
  preview?: FormulationPreview;
};

export type FormulationTemplate = {
  id: string;
  code: string;
  name: string;
  description: string;
  /** active | retired */
  state: string;
  state_label?: string;
  config?: FormulationTemplateConfig;
  /** 模板配置按现在的主数据（方法、能力、指标）重核的结果；只在详情里有 */
  check?: { ok: boolean; problems: string[] };
  /** 实验参数与逐瓶参数的规格；只在详情里有 */
  param_specs?: Record<string, FormulationParamSpec>;
  row_version: number;
  created_by_name?: string;
  created_at?: string | null;
  updated_at?: string | null;
};

export type FormulationColumn = {
  header: string;
  name: string;
  unit: string;
  kind: 'serial' | 'reagent' | 'param' | 'ignored';
  category?: string;
  stage?: string;
};

/** 表格里的一瓶：行号（表格里的第几行）、瓶身序列号、各试剂用量（按试剂名） */
export type FormulationRow = {
  row: number; serial: string; amounts: Record<string, number>;
  /** 逐瓶参数：参数 key → 这一瓶的值 */
  params?: Record<string, number | string>;
};

export type FormulationReagent = {
  name: string;
  category: string;
  stage: string;
  unit?: string;
  total: number;
  /** 用量为 0 的瓶数（这些瓶跳过这种料） */
  zero_rows: number;
};

export type FormulationPlanDraft = {
  name: string;
  plan_type: string;
  factors: Factor[];
  design_points: (number | string)[][];
  repeats: number;
  sample_ids: string[];
  required_metrics: string[];
  goal: string;
};

/** 解析 / 预览的结果：服务端按模板把表格翻译成流程步骤与方案，issues 非空就不能导入 */
export type FormulationPreview = {
  columns: FormulationColumn[];
  rows: FormulationRow[];
  reagents: FormulationReagent[];
  steps: RecipeStep[];
  bom: BomItem[];
  plan: FormulationPlanDraft;
  /** 生成的流程草稿的名称、样品位、风险评估编号与设计说明 */
  recipe?: { name: string; plate: number; risk: string; design: string };
  /** 实际用上的实验参数取值 */
  params?: Record<string, number | string>;
  issues: string[];
  warnings: string[];
  /** 读文件时的提醒（隐藏行、隐藏工作表）；只在上传解析的结果里有 */
  sheet_warnings?: string[];
  template_id?: string;
  /** 读出来的原始表格：后续预览与导入原样回传 */
  filename?: string;
  table?: (string | number | null)[][];
};

export type FormulationImportResult = {
  template_id: string;
  recipe: { id: string; name: string; state: string; state_label?: string; reused: boolean };
  plan: { id: string; name: string; state: string; state_label?: string };
  samples: { id: string; created: boolean }[];
  warnings: string[];
};
