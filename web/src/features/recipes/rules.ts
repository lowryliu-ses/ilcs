/* 编辑器的即时反馈规则，与后端 `domain/recipe_rules.py` 与 `domain/steps.py` 一一对应。

   这里算出来的只是提示：能不能保存、能不能提交评审由服务端那份说了算，
   界面拿到 409 会把服务端的理由显示出来。两边同时改，才不会出现
   "界面说通过、提交被拒" 的错位。

   判据按步骤类型分支：设备步骤看能力与参数，人工步骤看记录表单，
   等待步骤看等待方式，审核步骤看审核角色。不适用的字段不提示缺失——
   否则一个纯人工流程会被「没有可承接工位」挡住。 */
import type { BomItem, CapabilityRow, Check, RecipeStep, StationRow } from '../../shared/types';

export type CapabilityIndex = Record<string, CapabilityRow>;

export type StepKind = 'device' | 'manual' | 'wait' | 'review';

export const STEP_KINDS: [StepKind, string][] = [
  ['device', '设备'],
  ['manual', '人工'],
  ['wait', '等待'],
  ['review', '审核'],
];

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
  if (kind === 'wait' || kind === 'review') return false;
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

/** 与工位无关的完整性问题，对应后端 step_issues。 */
export function stepIssues(step: RecipeStep, capabilities: CapabilityIndex): string[] {
  const issues: string[] = [];
  if (!step.name?.trim()) issues.push('步骤名称为空');
  const kind = kindOf(step);

  if (kind === 'device') issues.push(...deviceIssues(step, capabilities));
  else if (kind === 'manual') issues.push(...manualIssues(step));
  else if (kind === 'wait') issues.push(...waitIssues(step));
  else issues.push(...reviewIssues(step));

  // 审核节点没有预定时长，其余三类都要
  if (kind !== 'review') {
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
  const issues = steps.map((step) => stepIssues(step, capabilities));
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
      detail: `${steps.length} 步（${byKind}），总时长 ${total} min`,
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
