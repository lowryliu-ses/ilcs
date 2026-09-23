/* 编辑器的即时反馈规则，与后端 `domain/recipe_rules.py` 与 `domain/steps.py` 一一对应。

   这里算出来的只是提示：能不能保存、能不能提交评审由服务端那份说了算，
   界面拿到 409 会把服务端的理由显示出来。两边同时改，才不会出现
   "界面说通过、提交被拒" 的错位。

   判据按步骤类型分支：设备步骤看能力与参数，人工步骤看记录表单，
   等待步骤看等待方式，审核步骤看审核角色。不适用的字段不提示缺失——
   否则一个纯人工流程会被「没有可承接工位」挡住。 */
import type { BomItem, CapabilityRow, Check, RecipeStep, StationRow } from '../../shared/types';

export type CapabilityIndex = Record<string, CapabilityRow>;

export type StepKind = 'device' | 'manual' | 'wait' | 'review' | 'gate' | 'split';

export const STEP_KINDS: [StepKind, string][] = [
  ['device', '设备'],
  ['manual', '人工'],
  ['wait', '等待'],
  ['review', '审核'],
  ['gate', '质检关卡'],
  ['split', '样本拆分'],
];

/** 系统即时判定 / 执行的节点：没有预定时长，也不占工位。 */
export const AUTOMATIC_KINDS: StepKind[] = ['review', 'gate', 'split'];

/** 旧步骤没有 kind：一律按设备步骤解释——它们本来就是。 */
export function kindOf(step: RecipeStep): StepKind {
  const kind = step.kind as StepKind | undefined;
  return kind && STEP_KINDS.some(([value]) => value === kind) ? kind : 'device';
}

/** 这一步要不要占工位。人工步骤只有声明了工位资源才占；等待默认不占。 */
export function needsStation(step: RecipeStep): boolean {
  const kind = kindOf(step);
  const resource = step.resource ?? {};
  if (kind === 'device') return true;
  if (kind === 'manual') return Boolean(resource.station || resource.capability);
  if (kind === 'wait') return Boolean(resource.holds_station);
  return false;
}

/** 只有显式声明消耗物料的步骤才要 BOM。默认是「不消耗」。 */
export function consumesMaterials(step: RecipeStep): boolean {
  const kind = kindOf(step);
  if (kind !== 'device' && kind !== 'manual') return false;
  return Boolean(step.consumes_materials);
}

export function indexCapabilities(rows: CapabilityRow[] | undefined): CapabilityIndex {
  return Object.fromEntries((rows ?? []).map((row) => [row.id, row]));
}

/** 全部工位对某个能力参数的并集区间。工位未定义该参数即不能承接。 */
export function paramRange(
  stations: StationRow[] | undefined,
  cap: string,
  key: string,
): [number, number] | null {
  const windows = (stations ?? [])
    .map((station) => station.limits?.[cap]?.[key])
    .filter((window): window is [number, number] => Array.isArray(window) && window.length === 2);
  if (!windows.length) return null;
  return [Math.min(...windows.map((w) => w[0])), Math.max(...windows.map((w) => w[1]))];
}

export function defaultParams(stations: StationRow[] | undefined, capability: CapabilityRow): Record<string, number | ''> {
  return Object.fromEntries(
    Object.keys(capability.params ?? {}).map((key) => {
      const range = paramRange(stations, capability.id, key);
      return [key, range ? Number(((range[0] + range[1]) / 2).toFixed(2)) : 0];
    }),
  );
}

export function stationsForStep(stations: StationRow[] | undefined, step: RecipeStep): StationRow[] {
  if (!needsStation(step)) return [];
  return (stations ?? []).filter((station) => {
    const implemented = station.limits?.[step.cap];
    if (!implemented) return false;
    return Object.entries(step.params ?? {}).every(([key, value]) => {
      const window = implemented[key];
      if (!window) return false;
      return typeof value === 'number' && value >= window[0] && value <= window[1];
    });
  });
}

function deviceIssues(step: RecipeStep, capabilities: CapabilityIndex): string[] {
  const issues: string[] = [];
  const capability = capabilities[step.cap];
  const defined = capability?.params ?? {};
  if (!capability) issues.push(`能力 ${step.cap || '未选择'} 未登记`);
  else if (capability.retired) issues.push(`能力「${capability.name}」已停用，不能用于新步骤`);

  Object.entries(defined).forEach(([key, paramLabel]) => {
    const value = step.params?.[key];
    if (typeof value !== 'number' || !Number.isFinite(value)) issues.push(`${paramLabel || key} 未填写`);
  });
  if (capability) {
    Object.keys(step.params ?? {}).forEach((key) => {
      if (!(key in defined)) issues.push(`参数 ${key} 不属于该能力`);
    });
  }
  return issues;
}

function manualIssues(step: RecipeStep): string[] {
  const fields = step.form ?? [];
  if (!fields.length) return ['人工步骤必须定义结构化记录表单，至少一个字段'];
  const issues: string[] = [];
  const seen = new Set<string>();
  fields.forEach((field, index) => {
    const key = (field.key ?? '').trim();
    if (!key) issues.push(`表单第 ${index + 1} 项缺少字段标识`);
    else if (seen.has(key)) issues.push(`表单字段标识 ${key} 重复`);
    else seen.add(key);
    if (!(field.label ?? '').trim()) issues.push(`表单字段 ${key || index + 1} 缺少显示名称`);
    if (field.type === 'enum' && !(field.options ?? []).length) {
      issues.push(`表单字段 ${key} 是枚举但没有可选值`);
    }
  });
  return issues;
}

function waitIssues(step: RecipeStep): string[] {
  const mode = step.wait_for?.mode ?? (step.dur ? 'duration' : '');
  if (mode === 'duration') {
    return typeof step.dur === 'number' && step.dur > 0 ? [] : ['等待时长必须大于 0'];
  }
  if (mode === 'event') {
    // 与服务端 wait_issues 同步：没有入口能发出业务事件，这种节点会永远等下去
    return ['「业务事件」等待暂不支持：当前没有可发出该事件的入口，请改用固定时长'];
  }
  return ['等待方式未选择（当前只支持固定时长）'];
}

function reviewIssues(step: RecipeStep): string[] {
  const role = (step.review_role ?? '').trim();
  if (!role) return ['审核步骤必须指定审核角色'];
  if (!['qa', 'researcher', 'admin'].includes(role)) return [`审核角色 ${role} 不在可选范围内`];
  return [];
}

const stepIdOf = (step: RecipeStep, index: number) => step.step_id || `s${String(index + 1).padStart(2, '0')}`;

/** 质检关卡，对应后端 gate_issues：测量来源是之前的设备步骤，返工目标不晚于测量来源。 */
function gateIssues(step: RecipeStep, steps: RecipeStep[], index: number): string[] {
  const gate = step.gate ?? {};
  const issues: string[] = [];
  const ids = steps.map(stepIdOf);
  const source = gate.source_step_id ?? '';
  const sourceIndex = ids.slice(0, index).indexOf(source);
  if (sourceIndex < 0) issues.push('质检关卡必须指定它之前的一个设备步骤作为测量来源');
  else if (kindOf(steps[sourceIndex]) !== 'device') issues.push('测量来源必须是设备步骤：只有设备回执里有测量值');
  if (!gate.field?.trim()) issues.push('质检关卡必须指定测量字段（设备回执 delivered 里的键）');
  const bounds = [gate.min, gate.max].filter((b): b is number => typeof b === 'number' && Number.isFinite(b));
  if (!bounds.length) issues.push('质检关卡至少要有下限或上限');
  else if (bounds.length === 2 && (gate.min as number) > (gate.max as number)) issues.push('质检关卡下限不能大于上限');
  if (!gate.on_fail) issues.push('不合格去向必须是返工、报废或保持待人工判断');
  if (gate.on_fail === 'rework') {
    const target = ids.slice(0, index).indexOf(gate.rework_to ?? '');
    if (target < 0) issues.push('返工必须回到关卡之前的某一步');
    else if (sourceIndex >= 0 && target > sourceIndex) issues.push('返工目标不能晚于测量来源：否则返工不会重新测量');
    const rounds = gate.max_rework;
    if (typeof rounds !== 'number' || !Number.isInteger(rounds) || rounds < 1 || rounds > 5) {
      issues.push('最多返工次数必须是 1–5 的整数；超过后转人工判断');
    }
  }
  return issues;
}

function splitIssues(step: RecipeStep): string[] {
  const split = step.split ?? {};
  const issues: string[] = [];
  const count = split.count;
  if (typeof count !== 'number' || !Number.isInteger(count) || count < 2 || count > 96) issues.push('拆分份数必须是 2–96 的整数');
  if (!split.child_type?.trim()) issues.push('必须写明子样本类型（如 扣电、极片）');
  return issues;
}

/* ---------- 依赖图，对应后端 domain/graph.py ---------- */

/** 任何一步声明了 after 就是依赖图模式；否则是顺序流程。 */
export function graphMode(steps: RecipeStep[]): boolean {
  return steps.some((step) => step.after !== undefined);
}

/** 每一步的前驱下标。未声明 after 的步骤依赖上一行；引用无效的忽略（由校验报出）。 */
export function predecessors(steps: RecipeStep[]): number[][] {
  const ids = steps.map(stepIdOf);
  const linear = !graphMode(steps);
  return steps.map((step, index) => {
    if (linear || step.after === undefined) return index > 0 ? [index - 1] : [];
    return [...new Set((step.after ?? []).map((ref) => ids.indexOf(ref)).filter((at) => at >= 0 && at < index))].sort(
      (a, b) => a - b,
    );
  });
}

export function ancestors(steps: RecipeStep[], index: number): Set<number> {
  const before = predecessors(steps);
  const seen = new Set<number>();
  const stack = [...before[index]];
  while (stack.length) {
    const current = stack.pop() as number;
    if (seen.has(current)) continue;
    seen.add(current);
    stack.push(...before[current]);
  }
  return seen;
}

export function criticalPathMin(steps: RecipeStep[]): number {
  const before = predecessors(steps);
  const finish: number[] = [];
  steps.forEach((step, index) => {
    const start = Math.max(0, ...before[index].map((parent) => finish[parent]));
    finish.push(start + (Number(step.dur) || 0));
  });
  return Math.max(0, ...finish);
}

function graphIssues(steps: RecipeStep[], index: number): string[] {
  const step = steps[index];
  if (!graphMode(steps) || step.after === undefined) return [];
  if (!Array.isArray(step.after)) return ['前驱步骤必须是步骤标识列表'];
  const ids = steps.map(stepIdOf);
  return step.after.flatMap((ref) => {
    if (ref === ids[index]) return ['步骤不能依赖自己'];
    const at = ids.indexOf(ref);
    if (at < 0) return [`前驱步骤 ${ref} 不存在`];
    if (at > index) return [`前驱步骤 ${ref}（第 ${at + 1} 步）排在本步之后：请把它移到前面`];
    return [];
  });
}

/** 与工位无关的完整性问题，对应后端 step_issues（关卡的跨步骤校验对应 validate_steps）。 */
export function stepIssues(
  step: RecipeStep,
  capabilities: CapabilityIndex,
  steps: RecipeStep[] = [step],
  index = 0,
): string[] {
  const issues: string[] = [];
  if (!step.name?.trim()) issues.push('步骤名称为空');
  const kind = kindOf(step);

  if (kind === 'device') issues.push(...deviceIssues(step, capabilities));
  else if (kind === 'manual') issues.push(...manualIssues(step));
  else if (kind === 'wait') issues.push(...waitIssues(step));
  else if (kind === 'gate') issues.push(...gateIssues(step, steps, index));
  else if (kind === 'split') issues.push(...splitIssues(step));
  else issues.push(...reviewIssues(step));
  issues.push(...graphIssues(steps, index));
  if (kind === 'gate' && graphMode(steps)) {
    const target = steps.map(stepIdOf).indexOf(step.gate?.rework_to ?? '');
    if (target >= 0 && !ancestors(steps, index).has(target)) issues.push('返工目标必须是本关卡的上游步骤（依赖链上的前驱）');
  }

  // 审核、质检关卡、样本拆分即时判定 / 登记，没有预定时长
  if (!AUTOMATIC_KINDS.includes(kind)) {
    if (typeof step.dur !== 'number' || !Number.isFinite(step.dur) || step.dur <= 0) {
      issues.push('计划时长必须大于 0');
    }
  }
  if (step.hard) {
    if (!step.hard.from?.trim()) issues.push('硬时限缺少起算事件');
    if (typeof step.hard.maxGapMin !== 'number' || step.hard.maxGapMin <= 0) issues.push('硬时限最长间隔必须大于 0');
  }
  return issues;
}

/** 方法级检查清单，对应后端 recipe_checks。前五项决定能否提交评审。 */
export function editorChecks(
  steps: RecipeStep[],
  bom: BomItem[],
  risk: string,
  stations: StationRow[] | undefined,
  capabilities: CapabilityIndex,
  sopVersionId = '',
): Check[] {
  const issues = steps.map((step, index) => stepIssues(step, capabilities, steps, index));
  const noStation = steps
    .map((step, index) => (!needsStation(step) || stationsForStep(stations, step).length ? null : index + 1))
    .filter((index): index is number => index !== null);
  const incomplete = issues
    .map((list, index) => (list.length ? index + 1 : null))
    .filter((index): index is number => index !== null);
  const total = steps.reduce((sum, step) => sum + (Number(step.dur) || 0), 0);
  const hardSteps = steps.filter((step) => step.hard);
  const hardOk = hardSteps.every((step) => step.hard?.from?.trim());
  const stationSteps = steps.filter(needsStation).length;
  const materialSteps = steps.filter(consumesMaterials).length;
  const byKind = STEP_KINDS.map(
    ([kind, label]) => `${label} ${steps.filter((step) => kindOf(step) === kind).length}`,
  ).join('、');

  return [
    {
      key: 'steps',
      label: '至少一个步骤',
      ok: steps.length > 0,
      detail: graphMode(steps)
        ? `${steps.length} 步（${byKind}），关键路径 ${criticalPathMin(steps)} min（各步合计 ${total} min，含并行分支）`
        : `${steps.length} 步（${byKind}），总时长 ${total} min`,
    },
    {
      key: 'stations',
      label: '需要占用的步骤都有可承接工位',
      ok: noStation.length === 0,
      detail: noStation.length
        ? `第 ${noStation.join('、')} 步参数超出全部工位极限`
        : stationSteps
        ? `${stationSteps} 个步骤需要工位，参数均在某一工位极限内`
        : '本流程没有需要工位的步骤',
    },
    {
      key: 'complete',
      label: '各类节点的适用字段完整',
      ok: incomplete.length === 0,
      detail: incomplete.length ? `第 ${incomplete.join('、')} 步填写不完整` : '无缺失',
    },
    {
      key: 'hard',
      label: '硬时限步骤有起算事件',
      ok: hardOk,
      detail: hardOk ? `${hardSteps.length} 步定义了硬时限` : '存在硬时限步骤未填写起算事件',
    },
    {
      key: 'bom',
      label: '物料需求（BOM）',
      // 合法空 BOM：没有声明消耗物料的步骤就不需要 BOM
      ok: bom.length > 0 || materialSteps === 0,
      detail: bom.length
        ? bom.map((item) => `${item.material} ${item.qty}${item.unit}`).join('、')
        : materialSteps === 0
        ? '无需物料：本流程没有消耗物料的步骤'
        : '存在消耗物料的步骤但未定义 BOM，排程前无法预留',
    },
    { key: 'risk', label: '风险评估编号', ok: true, detail: risk || '缺失：允许保存草稿，发布前必须补齐' },
    {
      key: 'sop',
      label: '关联 SOP 版本',
      ok: true,
      detail: sopVersionId || '未关联：允许保存草稿；需要受控作业指导的方法请补上',
    },
  ];
}

export const SUBMITTABLE_CHECKS = 5;

export function canSubmit(checks: Check[]): boolean {
  return checks.slice(0, SUBMITTABLE_CHECKS).every((check) => check.ok);
}
