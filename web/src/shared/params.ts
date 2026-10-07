/* 能力参数的规格与工位极限，与服务端 `domain/params.py` 一一对应。

   参数有三类：数值、整数、选项（溶剂种类、测试协议名、气氛）。选项型的值是登记的选项之一、原样作为文字下发，
   没有单位、不能比大小，所以工位极限写「允许哪些选项」而不是区间。
   前端只用它做即时提示与编辑器的控件选择，能不能下发由服务端那份说了算。 */
import type { CapabilityRow, LimitWindow, ParamSpec, ProgramCell, ProgramColumn, ProgramRow } from './types';
import { canonicalUnit } from './units';

export type ParamKind = 'number' | 'integer' | 'enum' | 'program';

export type ResolvedSpec = {
  label: string; type: ParamKind; unit: string; required: boolean; options: string[];
  /** 程序表：列定义与最多行数 */
  columns: ProgramColumn[]; maxRows: number;
};

export const MAX_PROGRAM_ROWS = 200;

/** 参数规格：没登记的按「数值、单位未登记、必填」——有规格之前的行为。 */
export function paramSpec(capability: Pick<CapabilityRow, 'params' | 'param_specs'> | undefined, key: string): ResolvedSpec {
  const raw: ParamSpec = capability?.param_specs?.[key] ?? {};
  const type: ParamKind =
    raw.type === 'integer' ? 'integer' : raw.type === 'enum' ? 'enum' : raw.type === 'program' ? 'program' : 'number';
  return {
    label: capability?.params?.[key] || key,
    type,
    unit: type === 'enum' || type === 'program' ? '' : canonicalUnit(raw.unit),
    required: raw.required !== false,
    options: type === 'enum' ? [...(raw.options ?? [])] : [],
    columns: type === 'program' ? [...(raw.columns ?? [])] : [],
    maxRows: type === 'program' && raw.max_rows ? raw.max_rows : MAX_PROGRAM_ROWS,
  };
}

export function isOptionWindow(window: LimitWindow | undefined): window is string[] {
  return Array.isArray(window) && window.length > 0 && window.every((item) => typeof item === 'string');
}

/** 程序表的一格是不是引用本步参数：{param: 'rate'} */
export function isRef(cell: unknown): cell is { param: string } {
  return typeof cell === 'object' && cell !== null && !Array.isArray(cell) && typeof (cell as { param?: unknown }).param === 'string';
}

function isColumnWindow(window: LimitWindow | undefined): window is Record<string, [number, number] | string[]> {
  return typeof window === 'object' && window !== null && !Array.isArray(window);
}

/** 程序表的字面格子是否都落在列极限里；没写极限的列不约束，引用的格子不按列查（与服务端 program.fits 一致） */
export function programFits(value: unknown, window: Record<string, [number, number] | string[]>): boolean {
  return programMisfits(value, window).length === 0 && Array.isArray(value);
}

export function programMisfits(value: unknown, window: Record<string, [number, number] | string[]>): string[] {
  if (!Array.isArray(value)) return ['不是程序表'];
  const reasons: string[] = [];
  value.forEach((row, index) => {
    Object.entries((row ?? {}) as ProgramRow).forEach(([column, cell]) => {
      const limit = window[column];
      if (!limit || isRef(cell) || cell === '' || cell === undefined) return;
      if (!windowFits(cell, limit)) {
        const shown = isOptionWindow(limit) ? limit.join('、') : `[${limit[0]}, ${limit[1]}]`;
        reasons.push(`第 ${index + 1} 行 ${column}=${String(cell)} 超出 ${shown}`);
      }
    });
  });
  return reasons;
}

/** 一个设定值落不落在工位极限里：数值按 [下限, 上限]，选项按「允许的选项」，程序表按列逐格。与服务端 window_fits 同一判据 */
export function windowFits(value: unknown, window: LimitWindow | undefined): boolean {
  if (isColumnWindow(window)) return programFits(value, window);
  if (!Array.isArray(window) || !window.length) return false;
  if (typeof value === 'string') return isOptionWindow(window) && window.includes(value);
  if (typeof value !== 'number' || !Number.isFinite(value) || isOptionWindow(window) || window.length !== 2) return false;
  const [low, high] = window as [number, number];
  return value >= low && value <= high;
}

/** 新接一项能力时的缺省极限：选项型全部允许，程序表不约束列，数值型 [0, 100]（与服务端登记能力时同一写法） */
export function defaultWindow(spec: ResolvedSpec): LimitWindow {
  if (spec.type === 'program') return {};
  return spec.type === 'enum' ? [...spec.options] : [0, 100];
}

/** 程序表一列当成单独参数看的规格（列极限、格子校验用） */
export function columnSpec(column: ProgramColumn): ResolvedSpec {
  const type: ParamKind = column.type === 'integer' ? 'integer' : column.type === 'enum' ? 'enum' : 'number';
  return {
    label: column.label || column.key, type, unit: type === 'enum' ? '' : canonicalUnit(column.unit), required: Boolean(column.required),
    options: type === 'enum' ? [...(column.options ?? [])] : [], columns: [], maxRows: MAX_PROGRAM_ROWS,
  };
}

/** 工位极限写法的问题；没问题返回空串。与服务端 limit_issues 对应 */
export function windowProblem(spec: ResolvedSpec, window: unknown): string {
  if (spec.type === 'program') {
    if (typeof window !== 'object' || window === null || Array.isArray(window)) return '程序表的极限要按列写';
    const columns = new Map(spec.columns.map((column) => [column.key, column]));
    for (const [key, limit] of Object.entries(window as Record<string, unknown>)) {
      const column = columns.get(key);
      if (!column) return `没有列 ${key}`;
      const problem = windowProblem(columnSpec(column), limit);
      if (problem) return `${column.label || key}：${problem}`;
    }
    return '';
  }
  if (spec.type === 'enum') {
    if (!Array.isArray(window) || !window.length || !window.every((item) => typeof item === 'string')) {
      return '至少允许一个选项';
    }
    const unknown = window.filter((item) => !spec.options.includes(item as string));
    return unknown.length ? `${unknown.join('、')} 不是能力登记的选项` : '';
  }
  if (!Array.isArray(window) || window.length !== 2) return '下限必须小于上限';
  const [low, high] = window;
  return typeof low === 'number' && typeof high === 'number' && low < high ? '' : '下限必须小于上限';
}

/** 一个已填的值是否符合规格；没问题返回空串。与服务端 value_issues 对应 */
export function valueProblem(spec: ResolvedSpec, value: unknown): string {
  if (spec.type === 'program') return programProblems(spec, value)[0] ?? '';
  if (spec.type === 'enum') {
    return typeof value === 'string' && spec.options.includes(value)
      ? ''
      : `${spec.label} 只能是 ${spec.options.join('、') || '（没有登记选项）'} 之一`;
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return `${spec.label} 必须是数值`;
  if (spec.type === 'integer' && !Number.isInteger(value)) return `${spec.label} 必须是整数`;
  return '';
}

/** 选项表的写法：用顿号、逗号或换行分隔，去掉空白与重复 */
export function parseOptions(text: string): string[] {
  const seen = new Set<string>();
  return text
    .split(/[、,，\n]+/)
    .map((item) => item.trim())
    .filter((item) => item && !seen.has(item) && (seen.add(item), true));
}

/** 一张程序表逐格的问题（与服务端 program.value_issues 对应）：行数、类型与选项、必填列、未定义的列 */
export function programProblems(spec: ResolvedSpec, value: unknown): string[] {
  if (!Array.isArray(value)) return [`${spec.label} 是程序表，要写成行的列表`];
  if (!value.length) return [`${spec.label} 至少要有一行`];
  const problems: string[] = [];
  if (value.length > spec.maxRows) problems.push(`${spec.label} 最多 ${spec.maxRows} 行（现在 ${value.length} 行）`);
  const columns = new Map(spec.columns.map((column) => [column.key, column]));
  value.forEach((raw, index) => {
    const where = `${spec.label}第 ${index + 1} 行`;
    const row = (raw ?? {}) as ProgramRow;
    Object.keys(row).forEach((key) => {
      if (!columns.has(key)) problems.push(`${where}的列 ${key} 不在程序表的列定义里`);
    });
    spec.columns.forEach((column) => {
      const cell = row[column.key] as ProgramCell | undefined;
      const label = `${where}的${column.label || column.key}`;
      if (cell === undefined || cell === '') {
        if (column.required) problems.push(`${label}必填`);
        return;
      }
      if (isRef(cell)) {
        if (column.type === 'enum') problems.push(`${label}是选项列，不能引用参数`);
        return;
      }
      const problem = valueProblem(columnSpec(column), cell);
      if (problem) problems.push(`${label}：${problem}`);
    });
  });
  return problems;
}

/** 程序表里引用到的本步参数名 */
export function programRefs(value: unknown): string[] {
  const found = new Set<string>();
  (Array.isArray(value) ? value : []).forEach((row) =>
    Object.values((row ?? {}) as ProgramRow).forEach((cell) => {
      if (isRef(cell)) found.add(cell.param);
    }),
  );
  return [...found];
}
