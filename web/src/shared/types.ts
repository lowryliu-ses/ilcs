/* 服务端契约的前端镜像。只写界面真正读的字段。 */

export type User = {
  id: string;
  username: string;
  display_name: string;
  role: string;
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
};

export type Gate = { open: boolean; reasons: string[]; degraded: string[]; checked_at: string };

/** 开跑检查项。`state` 区分通过、阻塞与不适用；`ok` 表示「不挡下发」。 */
export type Check = {
  key: string;
  label: string;
  ok: boolean;
  state?: 'pass' | 'blocked' | 'not_applicable';
  detail: string;
};

export type NextAction = { who: string; what: string; why: string };

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

export type StepRow = {
  index: number;
  step_id: string;
  kind: 'device' | 'manual' | 'wait' | 'review';
  kind_label: string;
  /** 人工 / 等待 / 审核节点默认不占工位，除非显式声明 */
  needs_station: boolean;
  name: string;
  cap: string;
  cap_name: string;
  params: Record<string, number>;
  dur: number;
  hard: { from?: string; maxGapMin?: number } | null;
  form: FormField[];
  wait_for: { mode?: string; event?: string };
  review_role: string;
  recovery: Recovery;
  station_id: string | null;
  planned_start: string | null;
  planned_end: string | null;
  transfer_station_id: string | null;
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
  /** 历史人工质量标记，不等于结果审核通过 */
  legacy_quality: string | null;
  flag_note: string;
  metrics: { areal_density: number | null; discharge_capacity: number | null; retention: number | null };
  raw_uri: string;
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
  /** 批次固化的 SOP 版本与附件摘要 */
  sop_snapshot: {
    sop_version_id?: string;
    code?: string;
    title?: string;
    version?: string;
    file_id?: string;
    file_checksum?: string;
  };
  steps: StepRow[];
  allocations: Allocation[];
  samples: AssignmentRow[];
  reservations: ReservationRow[];
  inventory_ledger: LedgerLine[];
  step_runs: StepRunRow[];
  workflow_events: WorkflowEventRow[];
  commands: CommandRow[];
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

export type Allocation = {
  step_index: number;
  station_id: string;
  kind: 'work' | 'transfer' | 'clean';
  starts_at: string;
  ends_at: string;
};

export type CommandRow = {
  /** queued | maybe_sent | delivered | unreachable。maybe_sent 禁止盲目重试 */
  delivery_state?: string;
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
};

export type BomItem = { material: string; qty: number; unit: string };

export type Factor = {
  name: string;
  unit: string;
  levels: (number | string)[];
  material?: { name: string; unit: string; per: number };
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
  delete_blockers: string[];
  /** 乐观并发版本；编辑保存与审批签名都绑定它 */
  row_version: number;
  author_user_id: string;
  submitted_by: string;
};

export type RecipeDetail = RecipeSummary & {
  steps: RecipeStep[];
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
    file_id: string;
    file_checksum: string;
    requires_training_ack: boolean;
  } | null;
};

/** 方法步骤。字段按 kind 分支适用：设备看 cap/params，人工看 form，
    等待看 wait_for，审核看 review_role。step_id 稳定不复用。 */
export type RecipeStep = {
  step_id?: string;
  kind?: 'device' | 'manual' | 'wait' | 'review';
  name: string;
  cap: string;
  params: Record<string, number | ''>;
  dur: number;
  hard?: { from: string; maxGapMin: number };
  form?: FormField[];
  wait_for?: { mode?: 'duration' | 'event'; event?: string };
  review_role?: string;
  requires_signature?: boolean;
  requires_sample_check?: boolean;
  consumes_materials?: boolean;
  resource?: { station?: string; capability?: string; holds_station?: boolean };
  qualification?: { sop?: string; safety?: string };
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

export type PlanDetail = PlanSummary & {
  factors: Factor[];
  control: { label: string; cond: (number | string)[] } | null;
  conditions: { group: string; levels: (number | string)[]; label: string; is_control: boolean }[];
  layout_preview: { well: string; group: string; repeat: number; label: string; is_control: boolean }[];
  checks: Check[];
  lockable: boolean;
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
  }[];
  metrics: { id: string; code: string; name: string; version: string; unit: string; value_type: string }[];
  audit: AuditRow[];
};

export type StationRow = {
  id: string;
  island: number;
  name: string;
  model: string;
  status: string;
  cal_due: string;
  positions: number;
  clean: boolean;
  limits: Record<string, Record<string, [number, number]>>;
  retired: boolean;
  retire_blockers: string[];
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

export type CapabilityRow = {
  id: string;
  name: string;
  params: Record<string, string>;
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

export type AccountRow = {
  id: string;
  username: string;
  display_name: string;
  role: 'researcher' | 'qa' | 'operator' | 'ehs' | 'admin';
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
    held: boolean;
    items: {
      batch_id: string;
      batch_state: string;
      step_index: number;
      step_name: string;
      kind: string;
      hard: { maxGapMin?: number } | null;
      starts_at: string;
      ends_at: string;
      uncertain: boolean;
    }[];
  }[];
  conflicts: { station_id: string; a: { batch_id: string }; b: { batch_id: string }; overlap_min: number }[];
};

export type QueueRow = {
  batch_id: string;
  recipe: string;
  version: string;
  priority: number;
  plan_id: string;
  material_ok: boolean;
  schedulable: boolean;
  blocker: string;
  makespan_min: number | null;
  path: { step_index: number; step_name: string; station_id: string; kind: string; starts_at: string; ends_at: string }[];
};

export type OptimizePreview = {
  baseline: { ok: boolean; order: string[]; span_min?: number; finish_at?: string; reason: string };
  best: { ok: boolean; order: string[]; span_min: number; finish_at: string };
  improvement_min: number | null;
  evaluated: number;
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
    typed_results: boolean;
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

export type GroupStat = {
  group: string;
  label: string;
  is_control: boolean;
  n_valid: number;
  n_total: number;
  mean: number | null;
  sd: number | null;
  cv_pct: number | null;
  areal_density: number | null;
  golden_mean: number | null;
  delta: number | null;
  samples: { id: string; well: string; repeat: number; state: string; quality: string | null; value: number | null }[];
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

export type Analysis = {
  batch_id: string;
  recipe_id: string;
  recipe_name: string;
  plan_id: string;
  plan_name: string;
  state: string;
  golden_batch_id: string;
  is_golden: boolean;
  groups: GroupStat[];
  effects: { factor: string; unit: string; levels: { level: number | string; mean: number | null; n: number }[]; range: number | null }[];
  summary: {
    groups: number;
    valid_samples: number;
    total_samples: number;
    median_cv_pct: number | null;
    high_cv_groups: number;
    single_repeat: boolean;
    best_group: string | null;
    best_mean: number | null;
    delta_vs_golden: number | null;
  };
  samples: AssignmentRow[];
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
  serial: string;
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
    legacy_quality: string | null;
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
};

/* ---------- 步骤运行 ---------- */

export type FormField = {
  key: string;
  label: string;
  type?: 'number' | 'text' | 'bool' | 'enum';
  required?: boolean;
  options?: (string | number)[];
  unit?: string;
};

export type StepRunRow = {
  id: string;
  batch_id: string;
  step_id: string;
  step_index: number;
  step_name: string;
  kind: 'device' | 'manual' | 'wait' | 'review';
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
  form_data: { values?: Record<string, unknown>; checks?: Record<string, boolean>; note?: string };
  requires_signature: boolean;
  review_role: string;
  wait_for: { mode?: string; event?: string };
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

export type MetricRow = {
  id: string;
  code: string;
  name: string;
  version: string;
  value_type: 'number' | 'text' | 'enum';
  unit: string;
  method_version: string;
  sample_types: string[];
  rules: { min?: number; max?: number; options?: (string | number)[] };
  state: string;
  referenced_by: number;
  numeric: boolean;
  editable: boolean;
  created_at: string;
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
    collected: boolean;
    not_measured: boolean;
  }[];
  collected: boolean;
  missing_metrics: string[];
  review_pending: number;
  values?: ResultValueRow[];
};

/* ---------- SOP ---------- */

export type SopVersionRow = {
  id: string;
  sop_id: string;
  code: string;
  title: string;
  version: string;
  state: string;
  state_label: string;
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
  acks?: { person_id: string; person_name: string; acked_at: string }[];
  using_recipes?: { id: string; name: string; version: string; state: string }[];
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
  conclusion: string;
  batch_id: string;
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
    best_group: string | null;
    best_mean: number | null;
  };
  excluded: DatasetGroup['excluded'];
};

export type AnalysisView = {
  batch_id: string;
  plan_id: string;
  plan_name: string;
  plan_type: string;
  recipe_id: string;
  recipe_name: string;
  state: string;
  official: boolean;
  scope_label: string;
  available_metrics: { id: string; code: string; name: string; unit: string; numeric: boolean }[];
  selected_metrics: string[];
  metrics: MetricBlock[];
  non_numeric_metrics: { metric_id: string; metric_name: string; value_type: string }[];
  show_factor_effects: boolean;
  /** 历史批次没有类型化结果时回落到旧视图 */
  legacy?: boolean;
  legacy_note?: string;
  groups?: unknown[];
  summary?: Record<string, unknown>;
  samples?: unknown[];
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

/* ---------- 文件 ---------- */

export type FileRow = {
  id: string;
  filename: string;
  media_type: string;
  byte_size: number;
  checksum: string;
  state: string;
  origin: string;
  ref_type: string;
  ref_id: string;
  note: string;
  uploaded_by: string;
  created_at: string;
  downloadable: boolean;
};
