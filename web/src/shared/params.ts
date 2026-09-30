/* 能力参数的规格与工位极限，与服务端 `domain/params.py` 一一对应。

   参数有三类：数值、整数、选项（溶剂种类、测试协议名、气氛）。选项型的值是登记的选项之一、原样作为文字下发，
   没有单位、不能比大小，所以工位极限写「允许哪些选项」而不是区间。
   前端只用它做即时提示与编辑器的控件选择，能不能下发由服务端那份说了算。 */
import type { CapabilityRow, LimitWindow, ParamSpec } from './types';
import { canonicalUnit } from './units';

export type ParamKind = 'number' | 'integer' | 'enum';

export const PARAM_KIND_LABEL: Record<ParamKind, string> = { number: '数值', integer: '整数', enum: '选项' };

export type ResolvedSpec = { label: string; type: ParamKind; unit: string; required: boolean; options: string[] };

/** 参数规格：没登记的按「数值、单位未登记、必填」——有规格之前的行为。 */
export function paramSpec(capability: Pick<CapabilityRow, 'params' | 'param_specs'> | undefined, key: string): ResolvedSpec {
  const raw: ParamSpec = capability?.param_specs?.[key] ?? {};
  const type: ParamKind = raw.type === 'integer' ? 'integer' : raw.type === 'enum' ? 'enum' : 'number';
  return {
    label: capability?.params?.[key] || key,
    type,
    unit: type === 'enum' ? '' : canonicalUnit(raw.unit),
    required: raw.required !== false,
    options: type === 'enum' ? [...(raw.options ?? [])] : [],
  };
}

export function isOptionWindow(window: LimitWindow | undefined): window is string[] {
  return Array.isArray(window) && window.length > 0 && window.every((item) => typeof item === 'string');
}

/** 一个设定值落不落在工位极限里：数值按 [下限, 上限]，选项按「允许的选项」。与服务端 window_fits 同一判据 */
export function windowFits(value: unknown, window: LimitWindow | undefined): boolean {
  if (!Array.isArray(window) || !window.length) return false;
  if (typeof value === 'string') return isOptionWindow(window) && window.includes(value);
  if (typeof value !== 'number' || !Number.isFinite(value) || isOptionWindow(window) || window.length !== 2) return false;
  const [low, high] = window as [number, number];
  return value >= low && value <= high;
}

/** 新接一项能力时的缺省极限：选项型全部允许，数值型 [0, 100]（与服务端登记能力时同一写法） */
export function defaultWindow(spec: ResolvedSpec): LimitWindow {
  return spec.type === 'enum' ? [...spec.options] : [0, 100];
}

/** 工位极限写法的问题；没问题返回空串。与服务端 limit_issues 对应 */
export function windowProblem(spec: ResolvedSpec, window: unknown): string {
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
