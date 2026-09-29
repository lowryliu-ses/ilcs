/* 单位的规范写法与换算判定，与服务端 `domain/params.py` 一一对应。

   只做同一量纲内的比例换算（g ↔ mg、mL ↔ μL）；温度这类不能按比例换算的量、表里没有的单位都只认同名。
   前端只用它做即时提示与显示，能不能换算、换出来是多少由服务端那份说了算。 */

const UNIT_TABLE: Record<string, [string, number | null]> = {
  kg: ['mass', 1000], g: ['mass', 1], mg: ['mass', 0.001], 'μg': ['mass', 0.000001],
  L: ['volume', 1000], mL: ['volume', 1], 'μL': ['volume', 0.001],
  m: ['length', 1000], cm: ['length', 10], mm: ['length', 1], 'μm': ['length', 0.001],
  h: ['time', 3600], min: ['time', 60], s: ['time', 1],
  A: ['current', 1], mA: ['current', 0.001], Ah: ['charge', 1], mAh: ['charge', 0.001],
  V: ['voltage', 1], mV: ['voltage', 0.001],
  bar: ['pressure', 100000], kPa: ['pressure', 1000], mbar: ['pressure', 100], Pa: ['pressure', 1],
  '℃': ['temperature', null], '%': ['fraction', null],
};
const UNIT_ALIASES: Record<string, string> = {
  ug: 'μg', ul: 'μL', ml: 'mL', l: 'L', um: 'μm', '°c': '℃', degc: '℃',
  sec: 's', mah: 'mAh', ah: 'Ah', ma: 'mA', mv: 'mV', kpa: 'kPa', pa: 'Pa',
};

export function canonicalUnit(unit: string | undefined | null): string {
  const text = (unit ?? '').trim().replace(/µ/g, 'μ');
  if (!text) return '';
  if (text in UNIT_TABLE) return text;
  return UNIT_ALIASES[text.toLowerCase()] ?? text;
}

export function convertible(source: string, target: string): boolean {
  const a = canonicalUnit(source);
  const b = canonicalUnit(target);
  if (!a || !b) return false;
  if (a === b) return true;
  const left = UNIT_TABLE[a];
  const right = UNIT_TABLE[b];
  return Boolean(left && right && left[0] === right[0] && left[1] != null && right[1] != null);
}

export function splitRatio(unit: string | undefined): [string, string] | null {
  const text = (unit ?? '').trim();
  if (text.split('/').length !== 2) return null;
  const [numerator, denominator] = text.split('/').map(canonicalUnit);
  return numerator && denominator ? [numerator, denominator] : null;
}

/** 界面与提示里的「名称（单位）」；名称里已经写了单位就不重复。 */
export function withUnit(label: string, unit: string): string {
  return !unit || label.includes(unit) ? label : `${label}（${unit}）`;
}
