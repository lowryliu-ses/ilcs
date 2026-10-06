/* 编辑器的即时反馈规则，与后端 `domain/recipe_rules.py` 与 `domain/steps.py` 一一对应。

   这里算出来的只是提示：能不能保存、能不能提交评审由服务端那份说了算，
   界面拿到 409 会把服务端的理由显示出来。两边同时改，才不会出现
   "界面说通过、提交被拒" 的错位。

   判据按步骤类型分支：设备步骤看能力与参数，人工步骤看记录表单，
   等待步骤看等待方式，审核步骤看审核角色。不适用的字段不提示缺失——
   否则一个纯人工流程会被「没有可承接工位」挡住。 */
import { isOptionWindow, paramSpec, programProblems, programRefs, valueProblem, windowFits } from '../../shared/params';
import type {
  BomItem, BranchCase, CapabilityRow, Check, ParamBinding, ProgramRow, RecipeStep, StationRow,
} from '../../shared/types';
import { canonicalUnit, convertible, splitRatio } from '../../shared/units';

export type CapabilityIndex = Record<string, CapabilityRow>;

export type StepKind = 'device' | 'manual' | 'wait' | 'review' | 'gate' | 'split' | 'merge' | 'branch' | 'subflow' | 'notify';

export const STEP_KINDS: [StepKind, string][] = [
  ['device', '设备'],
  ['manual', '人工'],
  ['wait', '等待'],
  ['review', '审核'],
  ['gate', '质检关卡'],
  ['split', '样本拆分'],
  ['merge', '样本合并'],
  ['branch', '条件分支'],
  ['subflow', '子流程'],
  ['notify', '消息通知'],
];

/** 系统即时判定 / 执行的节点：没有预定时长，也不占工位。子流程的时长来自它引用的方法。 */
export const AUTOMATIC_KINDS: StepKind[] = ['review', 'gate', 'split', 'merge', 'branch', 'subflow', 'notify'];

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

/** 步骤顶层的非 params 字段（投料物料等）。按 unknown 读：保存前的草稿里可能是任何值，校验要能说出来。 */
function rawField(step: RecipeStep, key: string): unknown {
  return (step as unknown as Record<string, unknown>)[key];
}

/** 这一步投的是哪种物料（BOM / 批号上的物料名称，精确匹配）。没勾「消耗物料」或没写就是空串。对应后端 steps.step_material。 */
export function stepMaterial(step: RecipeStep): string {
  if (!consumesMaterials(step)) return '';
  const material = rawField(step, 'material');
  return typeof material === 'string' && material.trim() ? material : '';
}

/** 这一步投的全部物料（按加料顺序）：投一种料的是 stepMaterial，一步投几种料的是 materials 里的那些。对应后端 steps.step_materials。 */
export function stepMaterials(step: RecipeStep): string[] {
  if (!consumesMaterials(step)) return [];
  const rows = rawField(step, 'materials');
  if (Array.isArray(rows) && rows.length) {
    return rows
      .map((row) => (row && typeof row === 'object' ? (row as Record<string, unknown>).material : undefined))
      .filter((name): name is string => typeof name === 'string' && Boolean(name.trim()));
  }
  const single = stepMaterial(step);
  return single ? [single] : [];
}

/** 投料物料的字段完整性，对应后端 steps.material_issues；用量参数是否属于能力在 deviceIssues 里判。 */
function materialIssues(step: RecipeStep): string[] {
  const issues: string[] = [];
  if ('material' in step) {
    const material = rawField(step, 'material');
    if (typeof material !== 'string' || !material.trim()) issues.push('物料名称必须是非空文字');
    else if (!consumesMaterials(step)) issues.push('声明了投料物料，但没有勾选「消耗物料」');
  }
  const param = rawField(step, 'material_param');
  if (param != null && param !== '' && kindOf(step) !== 'device') issues.push('只有设备步骤可以指定用量参数');
  if ('materials' in step) issues.push(...severalMaterialsIssues(step));
  return issues;
}

/** 一步投几种料（materials），对应后端 steps._materials_issues：只有设备步骤能这样写，每种料一项、写明用量参数，
    料与参数都不重复，和 material / material_param 二选一。 */
function severalMaterialsIssues(step: RecipeStep): string[] {
  const rows = rawField(step, 'materials');
  if (kindOf(step) !== 'device') return ['只有设备步骤能一步投几种料（materials）'];
  if (!Array.isArray(rows) || !rows.length) return ['materials 要写成 [{"material": 物料名, "param": 用量参数}…]，至少一种'];
  const issues: string[] = [];
  const single = rawField(step, 'material_param');
  if ('material' in step || (single != null && single !== '')) {
    issues.push('一步投几种料用 materials，不能同时写 material / material_param');
  }
  const names = new Set<string>();
  const params = new Set<string>();
  rows.forEach((row, index) => {
    const position = index + 1;
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      issues.push(`materials 第 ${position} 项必须是对象`);
      return;
    }
    const { material: name, param } = row as Record<string, unknown>;
    const label = typeof name === 'string' && name.trim() ? name : `第 ${position} 项`;
    if (typeof name !== 'string' || !name.trim()) issues.push(`materials 第 ${position} 项的物料名称必须是非空文字`);
    else if (names.has(name)) issues.push(`物料 ${name} 在 materials 里出现了两次`);
    else names.add(name);
    if (typeof param !== 'string' || !param.trim()) issues.push(`materials 里 ${label} 要写用量参数 param`);
    else if (params.has(param)) issues.push(`用量参数 ${param} 被两种料共用`);
    else params.add(param);
  });
  if (!consumesMaterials(step)) issues.push('声明了投料物料，但没有勾选「消耗物料」');
  return issues;
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
    .filter((window): window is [number, number] =>
      Array.isArray(window) && window.length === 2 && !isOptionWindow(window));
  if (!windows.length) return null;
  return [Math.min(...windows.map((w) => w[0])), Math.max(...windows.map((w) => w[1]))];
}

/** 程序表参数：全部工位在各列上的极限并集（数值取最宽的区间，选项取并集）；没写极限的列不在结果里 */
export function programLimits(
  stations: StationRow[] | undefined, cap: string, key: string,
): Record<string, [number, number] | string[]> {
  const out: Record<string, [number, number] | string[]> = {};
  (stations ?? []).forEach((station) => {
    const window = station.limits?.[cap]?.[key];
    if (typeof window !== 'object' || window === null || Array.isArray(window)) return;
    Object.entries(window).forEach(([column, limit]) => {
      const current = out[column];
      if (isOptionWindow(limit)) {
        out[column] = [...new Set([...(isOptionWindow(current) ? current : []), ...limit])];
      } else if (Array.isArray(limit) && limit.length === 2) {
        const [low, high] = limit as [number, number];
        out[column] = current && !isOptionWindow(current)
          ? [Math.min((current as [number, number])[0], low), Math.max((current as [number, number])[1], high)]
          : [low, high];
      }
    });
  });
  return out;
}

/** 选项型参数：全部工位允许的选项的并集。没有工位允许的选项排不上。 */
export function paramOptions(stations: StationRow[] | undefined, cap: string, key: string): string[] {
  const allowed = new Set<string>();
  (stations ?? []).forEach((station) => {
    const window = station.limits?.[cap]?.[key];
    if (isOptionWindow(window)) window.forEach((option) => allowed.add(option));
  });
  return [...allowed];
}

export function defaultParams(
  stations: StationRow[] | undefined, capability: CapabilityRow,
): Record<string, number | string | ProgramRow[]> {
  return Object.fromEntries(
    Object.keys(capability.params ?? {}).map((key) => {
      const spec = paramSpec(capability, key);
      if (spec.type === 'program') return [key, [] as ProgramRow[]]; // 程序表从空表开始编辑（或用设备方法的缺省程序表）
      if (spec.type === 'enum') {
        // 选项型：取第一个有工位允许的选项
        const allowed = paramOptions(stations, capability.id, key);
        return [key, spec.options.find((option) => allowed.includes(option)) ?? spec.options[0] ?? ''];
      }
      const range = paramRange(stations, capability.id, key);
      return [key, range ? Number(((range[0] + range[1]) / 2).toFixed(2)) : 0];
    }),
  );
}

/** 步骤引用设备方法时，工位还要满足：型号在适用清单里、驱动自报过的程序目录包含该程序（没报过目录不筛）。
    与服务端 `domain/methods.station_allows` 同一判据；`station.model` 是服务端给出的有效型号（关联了资产取资产登记的）。 */
export function methodBlocksStation(station: StationRow, step: RecipeStep): string[] {
  const method = step.method;
  if (!method?.id) return [];
  const reasons: string[] = [];
  const models = (method.instrument_models ?? []).filter((value) => value.trim());
  if (models.length && !models.includes(station.model)) reasons.push(`型号 ${station.model || '未登记'} 不在方法适用型号内`);
  const programs = (station.adapter?.catalog?.methods ?? []).map((row) => row.program);
  if (method.program && programs.length && !programs.includes('*') && !programs.includes(method.program)) {
    reasons.push(`设备未报告支持程序 ${method.program}`);
  }
  return reasons;
}

/** 人工步骤声明占用的工位：指定一台，或实现了某能力的任一台（不看参数范围）。对应后端 capability.resource_fits */
function resourceFits(station: StationRow, step: RecipeStep): boolean {
  if (station.retired) return false;
  const resource = step.resource ?? {};
  if (resource.station) return station.id === resource.station;
  if (resource.capability) return Boolean(station.limits?.[resource.capability]);
  return false;
}

/** 等待期间样本留在上一步的设备里：它占的就是能承接那个设备前驱的工位。 */
export function holdsStation(step: RecipeStep): boolean {
  return kindOf(step) === 'wait' && Boolean(step.resource?.holds_station);
}

export function stationsForStep(
  stations: StationRow[] | undefined, step: RecipeStep, steps?: RecipeStep[], index?: number,
): StationRow[] {
  if (!needsStation(step)) return [];
  if (kindOf(step) === 'manual') return (stations ?? []).filter((station) => resourceFits(station, step));
  if (holdsStation(step)) {
    if (!steps || index === undefined) return [];
    const parent = predecessors(steps)[index];
    const source = parent.length === 1 ? steps[parent[0]] : undefined;
    return source && kindOf(source) === 'device' ? stationsForStep(stations, source) : [];
  }
  return (stations ?? []).filter((station) => {
    const implemented = station.limits?.[step.cap];
    if (!implemented) return false;
    if (methodBlocksStation(station, step).length) return false;
    const fixed = Object.entries(step.params ?? {}).every(([key, value]) => windowFits(value, implemented[key]));
    // 取自上游结果的参数排程时还没有值：工位极限要覆盖整个预期范围（与服务端 bindings.window_holds 同一判据）
    return fixed && Object.entries(step.bindings ?? {}).every(([key, binding]) => {
      const window = implemented[key];
      const expect = expectOf(binding);
      if (!window || !expect || isOptionWindow(window)) return false;
      const [low, high] = window as [number, number];
      return expect[0] >= low && expect[1] <= high;
    });
  });
}

/* ---------- 参数规格与前馈（对应后端 domain/params.py 与 domain/bindings.py） ---------- */

/** 参数规格：没登记的按「数值、单位未登记、必填」——有规格之前的行为。选项型带 options */
export function specOf(capability: CapabilityRow | undefined, key: string) {
  return paramSpec(capability, key);
}


export function expectOf(binding: ParamBinding | undefined): [number, number] | null {
  const window = binding?.expect;
  if (!Array.isArray(window) || window.length !== 2) return null;
  const [low, high] = window;
  if (typeof low !== 'number' || typeof high !== 'number' || low > high) return null;
  return [low, high];
}

/** 前馈可选的来源：依赖图模式按祖先；顺序流程就是它前面的各步。 */
export function upstreamOf(steps: RecipeStep[], index: number): Set<number> {
  if (graphMode(steps)) return ancestors(steps, index);
  return new Set(Array.from({ length: index }, (_, i) => i));
}

function ratioUnitIssues(label: string, unit: string | undefined, sourceUnit: string, targetUnit: string): string[] {
  const ratio = splitRatio(unit);
  if (!ratio) return [`${label} 的系数单位要写成「目标单位/来源单位」，如 ${targetUnit || 'μL'}/${sourceUnit || 'mg'}`];
  const [numerator, denominator] = ratio;
  const issues: string[] = [];
  if (targetUnit && !convertible(numerator, targetUnit)) issues.push(`${label} 的系数单位 ${unit} 换算不到参数单位 ${targetUnit}`);
  if (sourceUnit && !convertible(sourceUnit, denominator)) issues.push(`${label} 的系数单位 ${unit} 与来源单位 ${sourceUnit} 对不上`);
  return issues;
}

/** 前馈配置是否完整、能否核对。与服务端 bindings.binding_issues 一一对应。 */
export function bindingIssues(step: RecipeStep, steps: RecipeStep[], index: number, capabilities: CapabilityIndex): string[] {
  const bindings = step.bindings;
  if (!bindings || !Object.keys(bindings).length) return [];
  if (kindOf(step) !== 'device') return ['只有设备步骤可以声明前馈参数'];
  const capability = capabilities[step.cap];
  const ids = steps.map(stepIdOf);
  const allowed = upstreamOf(steps, index);
  const methodRules = step.method?.params ?? {};
  const issues: string[] = [];
  Object.entries(bindings).forEach(([param, binding]) => {
    if (!capability || !(param in (capability.params ?? {}))) {
      issues.push(`前馈参数 ${param} 不是能力「${capability?.name ?? step.cap}」的参数`);
      return;
    }
    const spec = specOf(capability, param);
    const label = spec.label;
    if (param in (step.params ?? {})) issues.push(`${label} 已声明取自上游结果，不能再写固定值`);
    if (!spec.unit) issues.push(`${label} 没有登记单位：前馈要做单位换算，先在能力字典里给它登记单位`);
    const field = (binding.field ?? '').trim();
    const scope = binding.scope ?? 'batch';
    if (!field) issues.push(`${label} 必须指定来源字段`);
    const position = ids.indexOf(binding.source_step_id ?? '');
    if (position < 0) {
      issues.push(`${label} 的前馈来源步骤 ${binding.source_step_id || '未选择'} 不在流程里`);
    } else {
      const origin = steps[position];
      const name = origin.name || binding.source_step_id;
      const kind = kindOf(origin);
      if (!allowed.has(position)) issues.push(`${label} 的前馈来源「${name}」必须是本步的上游步骤`);
      if (kind !== 'device' && kind !== 'manual') {
        issues.push(`${label} 的前馈来源「${name}」只能是设备步骤（回执测量值）或人工步骤（记录字段）`);
      } else if (kind === 'manual' && field) {
        const entry = (origin.form ?? []).find((row) => row.key === field);
        if (!entry) issues.push(`${label} 的前馈字段 ${field} 不在来源人工步骤「${name}」的记录表单里`);
        else {
          if ((entry.type ?? 'text') !== 'number') issues.push(`${label} 的前馈字段 ${field} 必须是数值字段`);
          if (scope === 'sample' && !entry.per_sample) issues.push(`逐样本前馈要求来源字段 ${field} 按样本录入`);
          if (scope === 'batch' && entry.per_sample) issues.push(`来源字段 ${field} 是按样本录入的，前馈范围应选逐样本`);
        }
      } else if (kind === 'device' && field) {
        const rules = origin.method?.outputs ?? [];
        const rule = rules.find((row) => row.key === field);
        if (rules.length && !rule) {
          issues.push(`${label} 的前馈字段 ${field} 不在来源设备方法的输出规则里（可用：${rules.map((row) => row.key).join('、')}）`);
        }
        const declared = canonicalUnit(rule?.unit);
        const written = canonicalUnit(binding.unit);
        if (declared && written && declared !== written) {
          issues.push(`${label} 的来源单位 ${written} 与输出规则登记的单位 ${declared} 不同：回报值按 ${declared} 读`);
        }
      }
    }
    const sourceUnit = canonicalUnit(binding.unit);
    if (!sourceUnit) issues.push(`${label} 必须写明来源值的单位`);
    // 与服务端 _coefficient_issues 同一顺序：没写系数 → 纯单位换算；写了就要么是因子、要么是大于 0 的固定值
    const coefficient = binding.coefficient;
    if (!coefficient || !Object.keys(coefficient).length) {
      if (sourceUnit && spec.unit && !convertible(sourceUnit, spec.unit)) {
        issues.push(`${label}：来源单位 ${sourceUnit} 不能直接换算成 ${spec.unit}，请填写系数（单位写成 ${spec.unit}/${sourceUnit}）`);
      }
    } else {
      const factor = (coefficient.factor ?? '').trim();
      const hasValue = coefficient.value !== undefined && coefficient.value !== null && coefficient.value !== '';
      if (factor && hasValue) issues.push(`${label} 的系数只能二选一：写在流程里的固定值，或引用方案因子`);
      else if ('factor' in coefficient && !hasValue) {
        if (!factor) issues.push(`${label} 引用方案因子作系数时必须写明因子名`);
      } else if (typeof coefficient.value !== 'number' || !(coefficient.value > 0)) {
        issues.push(`${label} 的系数必须是大于 0 的数值`);
      } else issues.push(...ratioUnitIssues(label, coefficient.unit, sourceUnit, spec.unit));
    }
    const expect = expectOf(binding);
    if (!expect) issues.push(`${label} 必须填写预期范围（下限 ≤ 上限）：排程按它匹配工位极限`);
    else {
      const rule = methodRules[param];
      if (rule && ((rule.min != null && expect[0] < rule.min) || (rule.max != null && expect[1] > rule.max))) {
        issues.push(`${label} 的预期范围 [${expect[0]}, ${expect[1]}] 超出设备方法允许的 [${rule.min ?? '−∞'}, ${rule.max ?? '∞'}]`);
      }
    }
  });
  return issues;
}

function deviceIssues(step: RecipeStep, capabilities: CapabilityIndex): string[] {
  const issues: string[] = [];
  const capability = capabilities[step.cap];
  const defined = capability?.params ?? {};
  if (!capability) issues.push(`能力 ${step.cap || '未选择'} 未登记`);
  else if (capability.retired) issues.push(`能力「${capability.name}」已停用，不能用于新步骤`);

  const bound = new Set(Object.keys(step.bindings ?? {}));
  Object.keys(defined).forEach((key) => {
    if (bound.has(key)) return; // 取自上游结果，由 bindingIssues 核对
    const spec = specOf(capability, key);
    const value = step.params?.[key];
    if (
      value === undefined || value === '' || (Array.isArray(value) && !value.length) ||
      (typeof value === 'number' && !Number.isFinite(value))
    ) {
      if (spec.required) issues.push(`${spec.label} 未填写`);
      return;
    }
    if (spec.type === 'program') {
      issues.push(...programProblems(spec, value));
      // 引用本步参数的格子：引用的要是本能力的数值参数、单位相同，而且这一步给了它值（与服务端 program.ref_issues 对应）
      programRefs(value).forEach((ref) => {
        const target = specOf(capability, ref);
        if (ref === key || !(ref in defined)) issues.push(`${spec.label} 引用的参数 ${ref} 不是本能力的另一个参数`);
        else if (target.type !== 'number' && target.type !== 'integer') issues.push(`${spec.label} 引用的 ${target.label} 不是数值参数`);
        else if (!bound.has(ref) && (step.params?.[ref] === undefined || step.params?.[ref] === '')) {
          issues.push(`${spec.label} 引用的 ${target.label} 没有值：程序表下发时要代入它`);
        }
      });
      return;
    }
    const problem = valueProblem(spec, value);
    if (problem) issues.push(problem);
  });
  if (capability) {
    Object.keys(step.params ?? {}).forEach((key) => {
      if (!(key in defined)) issues.push(`参数 ${key} 不属于该能力`);
    });
    // 用量参数：执行器按它从下发参数里取投料量，再按物料单位对账，所以必须是登记了单位的能力参数
    const dosingParamIssues = (param: unknown, material: string) => {
      const owner = material ? `${material} 的` : '';
      if (typeof param !== 'string' || !(param in defined)) return [`${owner}用量参数 ${String(param)} 不是该能力的参数`];
      const type = specOf(capability, param).type;
      if (type === 'enum' || type === 'program') return [`${owner}用量参数 ${param} 不是数值参数，不能当投料量`];
      if (!specOf(capability, param).unit) return [`${owner}用量参数 ${param} 没有登记单位，无法与物料单位对账`];
      return [];
    };
    const materialParam = rawField(step, 'material_param');
    if (materialParam != null && materialParam !== '') issues.push(...dosingParamIssues(materialParam, ''));
    // 一步投几种料：每种料写明的用量参数同样要是登记了单位的数值参数
    const rows = rawField(step, 'materials');
    if (Array.isArray(rows)) {
      rows.forEach((row) => {
        if (!row || typeof row !== 'object') return;
        const { material, param } = row as Record<string, unknown>;
        if (param != null && param !== '') issues.push(...dosingParamIssues(param, typeof material === 'string' ? material : ''));
      });
    }
  }
  // 引用设备方法：参数必须落在方法允许的范围内（与服务端 `domain/methods.step_problems` 同源）
  const rules = step.method?.params ?? {};
  Object.entries(step.params ?? {}).forEach(([key, value]) => {
    const rule = rules[key];
    if (!rule) return;
    if (typeof value === 'string') {
      const allowed = rule.options ?? [];
      if (value && allowed.length && !allowed.includes(value)) {
        issues.push(`参数 ${key}=${value} 不在设备方法允许的选项 ${allowed.join('、')} 里`);
      }
      return;
    }
    if (typeof value !== 'number') return;
    if ((rule.min != null && value < rule.min) || (rule.max != null && value > rule.max)) {
      issues.push(`参数 ${key}=${value} 超出设备方法允许的 [${rule.min ?? '−∞'}, ${rule.max ?? '∞'}]`);
    }
  });
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
    if (field.per_sample && field.type !== 'number') issues.push(`表单字段 ${key} 按样本录入时必须是数值字段`);
  });
  return issues;
}

/* ---------- 工位资源、资质、按样本执行（对应后端 steps.resource_issues / qualification_issues /
   holds_station_issues / applies_to_issues） ---------- */

function resourceIssues(step: RecipeStep): string[] {
  const resource = step.resource;
  if (!resource || !Object.keys(resource).length) return [];
  const kind = kindOf(step);
  if (kind === 'manual') {
    if (resource.station && resource.capability) {
      return ['人工步骤占用的工位要么指定一台（station），要么按能力任一台（capability），不能两个都写'];
    }
    if (!resource.station && !resource.capability) {
      return ['人工步骤声明了工位资源，但没写占哪台工位（station）或哪种能力的工位（capability）'];
    }
    return [];
  }
  if (kind === 'wait') return [];
  if (kind === 'device') return [];
  return [`${STEP_KINDS.find(([value]) => value === kind)?.[1] ?? kind}步骤不占工位，不写工位资源`];
}

function qualificationIssues(step: RecipeStep): string[] {
  const required = step.qualification;
  if (!required || !Object.keys(required).length) return [];
  if (kindOf(step) !== 'manual') return ['只有人工步骤能声明执行人资质要求（设备步骤按能力自动要求）'];
  if (!required.sop?.trim() && !required.safety?.trim()) return ['声明了资质要求，但 SOP 与安全操作资质都没写'];
  return [];
}

function holdsStationIssues(steps: RecipeStep[], index: number): string[] {
  if (!holdsStation(steps[index])) return [];
  const before = predecessors(steps);
  const parent = before[index];
  if (parent.length !== 1) return ['等待期间占着工位：样本留在上一步的设备里，所以前驱只能有一个，而且要是设备步骤'];
  const source = steps[parent[0]];
  if (kindOf(source) !== 'device') {
    return [`等待期间占着工位：前驱「${source.name || stepIdOf(source, parent[0])}」不是设备步骤，样本不在设备里`];
  }
  const twins = steps.some(
    (other, at) => at !== index && holdsStation(other) && before[at].length === 1 && before[at][0] === parent[0],
  );
  return twins ? [`「${source.name || stepIdOf(source, parent[0])}」之后已经有一个等待步骤占着这台设备：样本只能在一处`] : [];
}

/** 能作按样本执行依据的投料步骤：指定了投料物料与用量参数的设备步骤 */
export function isDosingStep(step: RecipeStep): boolean {
  return kindOf(step) === 'device' && Boolean(step.material?.trim()) && Boolean(step.material_param);
}

function appliesToIssues(step: RecipeStep, steps: RecipeStep[], index: number): string[] {
  const rule = step.applies_to;
  if (!rule) return [];
  if (kindOf(step) !== 'device') return ['只有设备步骤能按样本限定处理对象（applies_to）'];
  const ids = steps.map(stepIdOf);
  const dosed = ids.indexOf(rule.dosed ?? '');
  const issues: string[] = [];
  if (dosed < 0 || dosed >= index) issues.push(`applies_to 引用的投料步骤 ${rule.dosed || '（未选）'} 不存在或不在本步之前`);
  else if (!isDosingStep(steps[dosed])) issues.push(`applies_to 引用的 ${rule.dosed} 不是指定了投料物料与用量参数的设备步骤`);
  (rule.then_any ?? []).forEach((ref) => {
    const at = ids.indexOf(ref);
    if (at < 0) issues.push(`applies_to 的 then_any 引用的步骤 ${ref} 不存在`);
    else if (dosed >= 0 && at <= dosed) issues.push(`applies_to 的 then_any 引用的 ${ref} 要排在 ${rule.dosed} 之后`);
    else if (!isDosingStep(steps[at])) issues.push(`applies_to 的 then_any 引用的 ${ref} 不是指定了投料物料与用量参数的设备步骤`);
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

/** 环境要求（与服务端 `domain/environment.requirement_issues` 同源）。 */
function environmentIssues(step: RecipeStep): string[] {
  const rows = step.environment;
  if (!rows) return [];
  const issues: string[] = [];
  rows.forEach((row, index) => {
    if (!row.metric?.trim()) {
      issues.push(`环境要求第 ${index + 1} 项没有指标`);
      return;
    }
    const low = row.min ?? null;
    const high = row.max ?? null;
    if (low === null && high === null) issues.push(`环境要求 ${row.metric} 没有上下限`);
    if (low !== null && high !== null && low > high) issues.push(`环境要求 ${row.metric} 下限大于上限`);
    if (!needsStation(step) && !row.zone?.trim()) issues.push(`环境要求 ${row.metric}：不占工位的步骤要写明区域`);
  });
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
    else if (c.loop_to && kindOf(steps[ids.indexOf(c.loop_to)]) === 'subflow') {
      issues.push(`${label} 回环目标不能是子流程节点：子流程建批次时展开，请指向具体步骤`);
    }
  });
  if (config.per_sample) {
    if (config.mode !== 'measure') issues.push('按样本分流只能按上游设备的测量值：每个样本要有自己孔位上的读数');
    if (cases.some((c) => c.loop_to)) issues.push('按样本分流的分支只能往前走、不能回环：各样本走的路不同，重做会把别的样本一起带回去');
  }
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
  if (!recipeId) return ['子流程必须选择引用的流程'];
  if (recipeId === selfId) return ['子流程不能引用流程自己'];
  if (!subflows) return [];
  const target = subflows[recipeId];
  if (!target) return [`子流程引用的流程 ${recipeId} 不存在`];
  if (target.state !== 'released' || target.needs_revision) return [`子流程引用的流程 ${recipeId}（${target.name}）不是有效的已发布版本`];
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
    else if (kindOf(steps[target]) === 'subflow') issues.push('返工目标不能是子流程节点：子流程建批次时展开，请指向具体步骤');
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
  const from = split.count_from;
  if (from && (from.factor || from.source_step_id || from.field)) {
    if (!from.factor && !(from.source_step_id && from.field)) {
      issues.push('按样本取份数要写方案因子（factor），或上游设备步骤与读数字段（source_step_id、field）');
    }
    if (count !== undefined && (typeof count !== 'number' || !Number.isInteger(count) || count < 1 || count > 96)) {
      issues.push('按样本取份数时，缺省份数（取不到时用）要是 1–96 的整数');
    }
  } else if (typeof count !== 'number' || !Number.isInteger(count) || count < 2 || count > 96) issues.push('拆分份数必须是 2–96 的整数');
  if (!split.child_type?.trim()) issues.push('必须写明子样本类型（如 扣电、极片）');
  return issues;
}

function mergeIssues(step: RecipeStep): string[] {
  const merge = step.merge ?? {};
  const issues: string[] = [];
  if (!['condition', 'all'].includes(merge.by ?? 'condition')) issues.push('合并方式只能是 condition（同一条件组合成一个）或 all（全部合成一个）');
  if (!merge.child_type?.trim()) issues.push('必须写明合并后的样本类型（如 合并液、粗品）');
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
  else if (kind === 'merge') issues.push(...mergeIssues(step));
  else if (kind === 'branch') issues.push(...branchIssues(step, steps, index));
  else if (kind === 'subflow') issues.push(...subflowIssues(step, subflows, selfId));
  else if (kind === 'notify') issues.push(...notifyIssues(step));
  else issues.push(...reviewIssues(step));
  issues.push(...materialIssues(step));
  issues.push(...resourceIssues(step));
  issues.push(...qualificationIssues(step));
  issues.push(...timeoutIssues(step));
  issues.push(...skippableIssues(step));
  issues.push(...environmentIssues(step));
  issues.push(...graphIssues(steps, index));
  issues.push(...bindingIssues(step, steps, index, capabilities));
  issues.push(...appliesToIssues(step, steps, index));
  issues.push(...holdsStationIssues(steps, index));
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
  sop?: SopLink,
  subflows?: SubflowIndex,
  selfId = '',
): Check[] {
  const issues = steps.map((step, index) => stepIssues(step, capabilities, steps, index, subflows, selfId));
  const noStation = steps
    .map((step, index) => (!needsStation(step) || stationsForStep(stations, step, steps, index).length ? null : index + 1))
    .filter((index): index is number => index !== null);
  const incomplete = issues
    .map((list, index) => (list.length ? index + 1 : null))
    .filter((index): index is number => index !== null);
  const total = steps.reduce((sum, step) => sum + durationOf(step, subflows), 0);
  const hardSteps = steps.filter((step) => step.hard);
  const hardOk = hardSteps.every((step) => step.hard?.from?.trim());
  const stationSteps = steps.filter(needsStation).length;
  const materialSteps = steps.filter(consumesMaterials);
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
    bomCheck(materialSteps, bom),
    { key: 'risk', label: '风险评估编号', ok: true, detail: risk || '缺失：允许保存草稿，发布前必须补齐' },
    sopCheck(steps, sop),
  ];
}

/** 对应后端 recipe_rules._bom_check。合法空 BOM 有两种：没有消耗物料的步骤；或每个消耗步骤都写明了投哪种料——
    这时每批的量随样本变，由实验方案的因子给出，建批次时按本批样本预留。
    例外是人工步骤：方案因子只能作用于设备步骤（参数要下发），人工步骤投的料没有因子能给出用量，只能按 BOM 预留，
    所以它投的物料不在 BOM 里就不能过——否则流程能发布，但任何方案都锁定不了。 */
function bomCheck(materialSteps: RecipeStep[], bom: BomItem[]): Check {
  const declared = [...new Set(materialSteps.flatMap(stepMaterials))];
  const base = { key: 'bom', label: '物料需求（BOM）' };
  const listed = new Set(bom.map((item) => item.material));
  const manualOutside = materialSteps
    .filter((step) => kindOf(step) !== 'device' && stepMaterial(step) && !listed.has(stepMaterial(step)))
    .map((step) => `人工步骤「${step.name || step.step_id || ''}」投的 ${stepMaterial(step)} 不在 BOM 里：人工步骤的用量只能按 BOM 预留，请把它加进 BOM`);
  if (manualOutside.length) return { ...base, ok: false, detail: manualOutside.join('；') };
  if (bom.length) {
    const outside = declared.filter((name) => !listed.has(name));
    const detail = bom.map((item) => `${item.material} ${item.qty}${item.unit}`).join('、');
    return { ...base, ok: true, detail: outside.length ? `${detail}；${outside.join('、')} 不在 BOM 里，用量由实验方案给出` : detail };
  }
  if (!materialSteps.length) return { ...base, ok: true, detail: '无需物料：本流程没有消耗物料的步骤' };
  if (materialSteps.every((step) => stepMaterials(step).length > 0)) {
    return { ...base, ok: true, detail: `${declared.join('、')} 的用量由实验方案按样本给出` };
  }
  return { ...base, ok: false, detail: '存在消耗物料的步骤但未定义 BOM，排程前无法预留' };
}

/** 流程关联的 SOP：给人看的名字、新批次实际会用的那一版的适用能力；problem 是后端判定的硬问题（如无生效版本）。 */
export type SopLink = { label: string; scope: string[]; problem?: string };

/** 对应后端 recipe_service.sop_problems：不关联允许；关联了就得有生效版本，设备能力在适用范围内。 */
function sopCheck(steps: RecipeStep[], sop?: SopLink): Check {
  if (!sop) {
    return { key: 'sop', label: '关联 SOP 版本', ok: true, detail: '未关联：允许保存草稿；需要受控作业指导的流程请补上' };
  }
  const outside = sop.scope.length
    ? [...new Set(steps.filter((step) => kindOf(step) === 'device' && step.cap && !sop.scope.includes(step.cap)).map((step) => step.cap))]
    : [];
  const problems = [
    ...(sop.problem ? [sop.problem] : []),
    ...(outside.length ? [`设备能力 ${outside.join('、')} 不在 SOP 适用范围内`] : []),
  ];
  return { key: 'sop', label: '关联 SOP 版本', ok: problems.length === 0, detail: [sop.label, ...problems].join('；') };
}

export const SUBMITTABLE_CHECKS = 5;

export function canSubmit(checks: Check[]): boolean {
  return checks.slice(0, SUBMITTABLE_CHECKS).every((check) => check.ok);
}
