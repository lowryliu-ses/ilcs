/* 编辑器的即时反馈规则，与后端 `domain/recipe_rules.py` 与 `domain/steps.py` 一一对应。

   这里算出来的只是提示：能不能保存、能不能提交评审由服务端那份说了算，
   界面拿到 409 会把服务端的理由显示出来。两边同时改，才不会出现
   "界面说通过、提交被拒" 的错位。

   判据按步骤类型分支：设备步骤看能力与参数，人工步骤看记录表单，
   等待步骤看等待方式，审核步骤看审核角色。不适用的字段不提示缺失——
   否则一个纯人工流程会被「没有可承接工位」挡住。 */
import type { BomItem, BranchCase, CapabilityRow, Check, RecipeStep, StationRow } from '../../shared/types';

export type CapabilityIndex = Record<string, CapabilityRow>;

export type StepKind = 'device' | 'manual' | 'wait' | 'review' | 'gate' | 'split' | 'branch' | 'subflow' | 'notify';

export const STEP_KINDS: [StepKind, string][] = [
  ['device', '设备'],
  ['manual', '人工'],
  ['wait', '等待'],
  ['review', '审核'],
  ['gate', '质检关卡'],
  ['split', '样本拆分'],
  ['branch', '条件分支'],
  ['subflow', '子流程'],
  ['notify', '消息通知'],
];

/** 系统即时判定 / 执行的节点：没有预定时长，也不占工位。子流程的时长来自它引用的方法。 */
export const AUTOMATIC_KINDS: StepKind[] = ['review', 'gate', 'split', 'branch', 'subflow', 'notify'];

/** 被子流程引用的方法：编辑器用它校验引用、算关键路径。对应后端展开时查的那些字段。 */
export type SubflowIndex = Record<string, { name: string; version: string; state: string; needs_revision: boolean; critical_path_min: number }>;

export const TIMEOUT_ACTIONS: [string, string][] = [
  ['alarm', '只报警'],
  ['fail', '判为失败，进入恢复评估'],
  ['skip', '自动跳过（需允许跳过）'],
];
/** 各类节点允许的超时处理。设备步骤只报警：物理动作是否完成由回执或现场核查决定。 */
export const TIMEOUT_ACTIONS_BY_KIND: Partial<Record<StepKind, string[]>> = {
  device: ['alarm'],
  manual: ['alarm', 'fail', 'skip'],
  wait: ['alarm', 'fail', 'skip'],
  review: ['alarm', 'fail', 'skip'],
  branch: ['alarm', 'fail'],
};
export const SKIPPABLE_KINDS: StepKind[] = ['device', 'manual', 'wait', 'review'];
export const MAX_LOOPS = 10;

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
    // 与服务端 wait_issues 同步：事件由批次信号入口发出，事件名要写明，排程按计划时长预留
    const issues: string[] = [];
    const name = (step.wait_for?.event ?? '').trim();
    if (!name) issues.push('业务事件等待必须写明事件名（如 sample_received、qc_released）');
    else if (!/^[A-Za-z0-9_\-.:\u4e00-\u9fff]{1,64}$/.test(name)) issues.push('事件名只能包含字母、数字与 _ - . :，最长 64 个字符');
    if (!(typeof step.dur === 'number' && step.dur > 0)) issues.push('业务事件等待也要填计划时长（排程按它预留时间）');
    return issues;
  }
  return ['等待方式未选择：固定时长或业务事件'];
}

function timeoutIssues(step: RecipeStep): string[] {
  const timeout = step.timeout;
  if (!timeout) return [];
  const kind = kindOf(step);
  const allowed = TIMEOUT_ACTIONS_BY_KIND[kind];
  if (!allowed) return [`${STEP_KINDS.find(([k]) => k === kind)?.[1] ?? kind}节点由系统即时处理，不支持超时配置`];
  const issues: string[] = [];
  if (!(typeof timeout.minutes === 'number' && timeout.minutes > 0)) issues.push('超时时长必须大于 0 分钟');
  const action = timeout.action || 'alarm';
  if (!allowed.includes(action)) {
    issues.push(kind === 'device' ? '设备步骤超时只能报警：物理动作是否完成由设备回执或现场核查决定' : '该节点不支持这种超时处理');
  }
  if (action === 'skip' && !step.skippable) issues.push('超时自动跳过要求本步骤允许跳过（skippable）');
  if (kind === 'wait' && (step.wait_for?.mode ?? 'duration') === 'duration') issues.push('固定时长等待到点即结束，不需要超时；业务事件等待才需要');
  return issues;
}

function skippableIssues(step: RecipeStep): string[] {
  if (!step.skippable) return [];
  return SKIPPABLE_KINDS.includes(kindOf(step)) ? [] : ['该类节点不能设为可跳过'];
}

export function branchCases(step: RecipeStep): BranchCase[] {
  return (step.branch?.cases ?? []).filter((c) => c && typeof c === 'object');
}

/** 不回环的出口：只有它们能出现在后继的 when 上。 */
export function forwardCaseKeys(step: RecipeStep): string[] {
  return branchCases(step).filter((c) => c.key && !c.loop_to).map((c) => c.key);
}

const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

/** 条件分支的字段完整性，对应后端 branch_issues。 */
function branchIssues(step: RecipeStep, steps: RecipeStep[], index: number): string[] {
  const config = step.branch ?? {};
  const issues: string[] = [];
  const mode = config.mode ?? '';
  if (!['measure', 'form', 'manual'].includes(mode)) issues.push('分支依据只能是上游测量值、上游人工记录字段或人工选择');
  const ids = steps.map(stepIdOf);
  if (mode === 'measure' || mode === 'form') {
    const source = config.source_step_id ?? '';
    const at = ids.slice(0, index).indexOf(source);
    if (at < 0) issues.push('分支必须指定它之前的一个步骤作为判据来源');
    else {
      const sourceKind = kindOf(steps[at]);
      if (mode === 'measure' && sourceKind !== 'device') issues.push('按测量值分支时来源必须是设备步骤：只有设备回执里有测量值');
      if (mode === 'form' && sourceKind !== 'manual') issues.push('按记录字段分支时来源必须是人工步骤');
      if (mode === 'form' && !(steps[at].form ?? []).some((f) => f.key === config.field)) issues.push('判据字段不在来源人工步骤的记录表单里');
    }
    if (!(config.field ?? '').trim()) issues.push('必须指定判据字段');
  }
  const cases = branchCases(step);
  if (cases.length < 2) issues.push('条件分支至少要有两个出口');
  const seen = new Set<string>();
  cases.forEach((c, position) => {
    const key = (c.key ?? '').trim();
    const label = `出口 ${key || position + 1}`;
    if (!key) issues.push(`第 ${position + 1} 个出口缺少标识`);
    else if (seen.has(key)) issues.push(`出口标识 ${key} 重复`);
    seen.add(key);
    if (!(c.label ?? '').trim()) issues.push(`${label} 缺少显示名称`);
    if (mode === 'measure' || mode === 'form') {
      const numeric = [c.min, c.max].filter(isNumber);
      const hasEquals = c.equals !== undefined && c.equals !== '';
      if (!numeric.length && !hasEquals && key !== (config.default ?? '')) issues.push(`${label} 没有判定条件（下限 / 上限 / 等于）；兜底出口请设为默认`);
      if (numeric.length === 2 && (c.min as number) > (c.max as number)) issues.push(`${label} 下限不能大于上限`);
    }
    if (c.loop_to && !ids.slice(0, index).includes(c.loop_to)) issues.push(`${label} 回环目标必须是分支之前的步骤`);
  });
  const fallback = config.default ?? '';
  if (fallback && !seen.has(fallback)) issues.push(`默认出口 ${fallback} 不存在`);
  if (fallback && cases.some((c) => c.key === fallback && c.loop_to)) issues.push('默认出口不能是回环：判据缺失时不应自动重做上游步骤');
  if (cases.some((c) => c.loop_to)) {
    const rounds = config.max_loops;
    if (!(isNumber(rounds) && Number.isInteger(rounds) && rounds >= 1 && rounds <= MAX_LOOPS)) {
      issues.push(`有回环出口时必须设置最多循环次数（1–${MAX_LOOPS}）；超过后转人工选择`);
    }
    if (!forwardCaseKeys(step).length) issues.push('至少要有一个不回环的出口，否则流程永远出不了循环');
  }
  return issues;
}

function notifyIssues(step: RecipeStep): string[] {
  const message = (step.notify?.message ?? '').trim();
  if (!message) return ['消息通知节点必须写明通知内容'];
  if (message.length > 500) return ['通知内容最长 500 个字符'];
  return [];
}

function subflowIssues(step: RecipeStep, subflows: SubflowIndex | undefined, selfId: string): string[] {
  const recipeId = step.subflow?.recipe_id ?? '';
  if (!recipeId) return ['子流程必须选择引用的方法'];
  if (recipeId === selfId) return ['子流程不能引用方法自己'];
  if (!subflows) return [];
  const target = subflows[recipeId];
  if (!target) return [`子流程引用的方法 ${recipeId} 不存在`];
  if (target.state !== 'released' || target.needs_revision) return [`子流程引用的方法 ${recipeId}（${target.name}）不是有效的已发布版本`];
  return [];
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

/** 任何一步声明了 after、或有条件分支，就是依赖图模式；否则是顺序流程。 */
export function graphMode(steps: RecipeStep[]): boolean {
  return steps.some((step) => step.after !== undefined || kindOf(step) === 'branch');
}

export function whenOf(step: RecipeStep): Record<string, string> {
  return step.when && typeof step.when === 'object' ? step.when : {};
}

export { stepIdOf };

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

export function successors(steps: RecipeStep[]): number[][] {
  const result: number[][] = steps.map(() => []);
  predecessors(steps).forEach((before, index) => before.forEach((parent) => result[parent].push(index)));
  return result;
}

export function descendants(steps: RecipeStep[], index: number): Set<number> {
  const after = successors(steps);
  const seen = new Set<number>();
  const stack = [...after[index]];
  while (stack.length) {
    const current = stack.pop() as number;
    if (seen.has(current)) continue;
    seen.add(current);
    stack.push(...after[current]);
  }
  return seen;
}

/** 回环体：目标步骤，加上目标的下游里同时是分支上游的那些。对应后端 graph.loop_body。 */
export function loopBody(steps: RecipeStep[], branchIndex: number, targetIndex: number): Set<number> {
  const upstream = ancestors(steps, branchIndex);
  return new Set([targetIndex, ...[...descendants(steps, targetIndex)].filter((at) => upstream.has(at))]);
}

/** 步骤的计划时长；子流程节点按引用方法的关键路径算。 */
export function durationOf(step: RecipeStep, subflows?: SubflowIndex): number {
  if (kindOf(step) === 'subflow') return subflows?.[step.subflow?.recipe_id ?? '']?.critical_path_min ?? 0;
  return Number(step.dur) || 0;
}

export function criticalPathMin(steps: RecipeStep[], subflows?: SubflowIndex): number {
  const before = predecessors(steps);
  const finish: number[] = [];
  steps.forEach((step, index) => {
    const start = Math.max(0, ...before[index].map((parent) => finish[parent]));
    finish.push(start + durationOf(step, subflows));
  });
  return Math.max(0, ...finish);
}

/* ---------- 编辑器的图操作：拖线建依赖、删边、删点、重排 ---------- */

/** 把「未声明就依赖上一行」写成显式 after。改图之前先这样做，否则插进来的步骤会改变隐式依赖。 */
export function explicitAfter(steps: RecipeStep[]): RecipeStep[] {
  const ids = steps.map(stepIdOf);
  const before = predecessors(steps);
  return steps.map((step, index) => ({ ...step, after: before[index].map((parent) => ids[parent]) }));
}

/** 稳定的拓扑排序：列表就是拓扑序（后端要求前驱排在前面），同层保持原来的相对顺序。 */
export function topoSort(steps: RecipeStep[]): RecipeStep[] {
  const ids = steps.map(stepIdOf);
  const position = new Map(ids.map((id, index) => [id, index]));
  const indegree = steps.map((step) => (step.after ?? []).filter((ref) => position.has(ref)).length);
  const children: number[][] = steps.map(() => []);
  steps.forEach((step, index) => (step.after ?? []).forEach((ref) => {
    const parent = position.get(ref);
    if (parent !== undefined) children[parent].push(index);
  }));
  const ready = steps.map((_, index) => index).filter((index) => indegree[index] === 0);
  const order: number[] = [];
  while (ready.length) {
    ready.sort((a, b) => a - b);
    const next = ready.shift() as number;
    order.push(next);
    children[next].forEach((child) => {
      indegree[child] -= 1;
      if (indegree[child] === 0) ready.push(child);
    });
  }
  if (order.length !== steps.length) return steps; // 有环：原样返回，由调用方拒绝这次改动
  return order.map((index) => steps[index]);
}

/** 连 from → to 会不会成环（to 已经是 from 的上游）。 */
export function wouldCycle(steps: RecipeStep[], fromId: string, toId: string): boolean {
  if (fromId === toId) return true;
  const ids = steps.map(stepIdOf);
  const from = ids.indexOf(fromId);
  const to = ids.indexOf(toId);
  if (from < 0 || to < 0) return true;
  return to === from || ancestors(steps, from).has(to);
}

/** 本地给新步骤分配标识：避开用过的（含已删除的），与服务端 assign_step_ids 同一格式。 */
export function nextStepId(steps: RecipeStep[], used: string[] = []): string {
  const taken = new Set([...used, ...steps.map(stepIdOf)]);
  let counter = 0;
  taken.forEach((id) => {
    const match = /^s(\d+)$/.exec(id);
    if (match) counter = Math.max(counter, Number(match[1]));
  });
  let candidate = '';
  do {
    counter += 1;
    candidate = `s${String(counter).padStart(2, '0')}`;
  } while (taken.has(candidate));
  return candidate;
}

/** 分支的下一个还没有后继使用的前进出口；都用过了就给第一个。 */
export function freeCase(steps: RecipeStep[], branchId: string): string {
  const branch = steps.find((step, index) => stepIdOf(step, index) === branchId);
  if (!branch) return '';
  const keys = forwardCaseKeys(branch);
  const used = new Set(steps.map((step) => whenOf(step)[branchId]).filter(Boolean));
  return keys.find((key) => !used.has(key)) ?? keys[0] ?? '';
}

/** 条件分支在依赖图上的约束，对应后端 graph.branch_graph_issues。按步骤下标给出。 */
export function branchGraphIssues(steps: RecipeStep[]): Record<number, string[]> {
  const ids = steps.map(stepIdOf);
  const before = predecessors(steps);
  const after = successors(steps);
  const issues: Record<number, string[]> = {};
  const push = (index: number, text: string) => (issues[index] ??= []).push(text);
  steps.forEach((step, index) => {
    Object.entries(whenOf(step)).forEach(([branchId, key]) => {
      const at = ids.indexOf(branchId);
      if (at < 0 || !before[index].includes(at)) push(index, `出口条件引用的 ${branchId} 不是本步的前驱`);
      else if (kindOf(steps[at]) !== 'branch') push(index, `前驱 ${branchId} 不是条件分支，不能带出口条件`);
      else if (!forwardCaseKeys(steps[at]).includes(key)) push(index, `分支「${steps[at].name || branchId}」没有可前进的出口 ${key}`);
    });
  });
  steps.forEach((step, index) => {
    if (kindOf(step) !== 'branch') return;
    const name = step.name || ids[index];
    after[index].forEach((child) => {
      if (!(ids[index] in whenOf(steps[child]))) push(child, `本步是分支「${name}」的后继，必须指定走哪个出口`);
    });
    const upstream = ancestors(steps, index);
    const config = step.branch ?? {};
    const source = config.source_step_id ?? '';
    if ((config.mode === 'measure' || config.mode === 'form') && ids.includes(source) && !upstream.has(ids.indexOf(source))) {
      push(index, '判据来源必须在分支的上游依赖链上（沿前驱能走到）');
    }
    branchCases(step).filter((c) => c.loop_to).forEach((c) => {
      const target = ids.indexOf(c.loop_to as string);
      if (target < 0) return;
      if (!upstream.has(target)) {
        push(index, `回环目标 ${c.loop_to} 必须是分支的上游步骤`);
        return;
      }
      const body = loopBody(steps, index, target);
      for (const member of [...body].sort((a, b) => a - b)) {
        const leak = after[member].find((child) => !body.has(child) && child !== index);
        if (leak !== undefined) {
          push(index, `回环体内第 ${member + 1} 步有通往回环外的后继（第 ${leak + 1} 步）：重做时体外步骤会失去前提，请把它挪到分支之后`);
          break;
        }
      }
    });
  });
  return issues;
}

function graphIssues(steps: RecipeStep[], index: number): string[] {
  const step = steps[index];
  if (!graphMode(steps)) return [];
  const branchProblems = branchGraphIssues(steps)[index] ?? [];
  if (step.after === undefined) return branchProblems;
  if (!Array.isArray(step.after)) return ['前驱步骤必须是步骤标识列表'];
  const ids = steps.map(stepIdOf);
  return [...branchProblems, ...step.after.flatMap((ref) => {
    if (ref === ids[index]) return ['步骤不能依赖自己'];
    const at = ids.indexOf(ref);
    if (at < 0) return [`前驱步骤 ${ref} 不存在`];
    if (at > index) return [`前驱步骤 ${ref}（第 ${at + 1} 步）排在本步之后：请把它移到前面`];
    return [];
  })];
}

/** 与工位无关的完整性问题，对应后端 step_issues（关卡的跨步骤校验对应 validate_steps）。 */
export function stepIssues(
  step: RecipeStep,
  capabilities: CapabilityIndex,
  steps: RecipeStep[] = [step],
  index = 0,
  subflows?: SubflowIndex,
  selfId = '',
): string[] {
  const issues: string[] = [];
  if (!step.name?.trim()) issues.push('步骤名称为空');
  const kind = kindOf(step);

  if (kind === 'device') issues.push(...deviceIssues(step, capabilities));
  else if (kind === 'manual') issues.push(...manualIssues(step));
  else if (kind === 'wait') issues.push(...waitIssues(step));
  else if (kind === 'gate') issues.push(...gateIssues(step, steps, index));
  else if (kind === 'split') issues.push(...splitIssues(step));
  else if (kind === 'branch') issues.push(...branchIssues(step, steps, index));
  else if (kind === 'subflow') issues.push(...subflowIssues(step, subflows, selfId));
  else if (kind === 'notify') issues.push(...notifyIssues(step));
  else issues.push(...reviewIssues(step));
  issues.push(...timeoutIssues(step));
  issues.push(...skippableIssues(step));
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
  subflows?: SubflowIndex,
  selfId = '',
): Check[] {
  const issues = steps.map((step, index) => stepIssues(step, capabilities, steps, index, subflows, selfId));
  const noStation = steps
    .map((step, index) => (!needsStation(step) || stationsForStep(stations, step).length ? null : index + 1))
    .filter((index): index is number => index !== null);
  const incomplete = issues
    .map((list, index) => (list.length ? index + 1 : null))
    .filter((index): index is number => index !== null);
  const total = steps.reduce((sum, step) => sum + durationOf(step, subflows), 0);
  const hardSteps = steps.filter((step) => step.hard);
  const hardOk = hardSteps.every((step) => step.hard?.from?.trim());
  const stationSteps = steps.filter(needsStation).length;
  const materialSteps = steps.filter(consumesMaterials).length;
  const byKind = STEP_KINDS.map(([kind, label]) => [label, steps.filter((step) => kindOf(step) === kind).length] as const)
    .filter(([, count], position) => count > 0 || position < 4)
    .map(([label, count]) => `${label} ${count}`)
    .join('、');

  return [
    {
      key: 'steps',
      label: '至少一个步骤',
      ok: steps.length > 0,
      detail: graphMode(steps)
        ? `${steps.length} 步（${byKind}），关键路径 ${criticalPathMin(steps, subflows)} min（各步合计 ${total} min，含并行与分支）`
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
