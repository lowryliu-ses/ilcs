import { displayTimezone } from './api';

/**
 * 历史接口返回的是“无时区的 UTC ISO 字符串”。在后端完成带时区迁移前，统一在
 * HTTP 展示边界补 Z；日期值（YYYY-MM-DD）不处理，避免被当成前一日。
 */
function normalizeInstant(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T/.test(value)) return value;
  return /(Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
}

export function dateOf(iso: string): Date {
  return new Date(normalizeInstant(iso));
}

export function clock(iso?: string | null): string {
  if (!iso) return '—';
  return dateOf(iso).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    timeZone: displayTimezone.get(),
  });
}

/** 带年份的日期：校准到期这类一年以上的期限只写月日会看成「昨天就到期了」 */
export function day(iso?: string | null): string {
  if (!iso) return '—';
  return dateOf(iso).toLocaleDateString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', timeZone: displayTimezone.get(),
  });
}

/** 带年份的日期时间 */
export function stamp(iso?: string | null): string {
  if (!iso) return '—';
  return dateOf(iso).toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    timeZone: displayTimezone.get(),
  });
}

export function time(iso?: string | null): string {
  if (!iso) return '—';
  return dateOf(iso).toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    timeZone: displayTimezone.get(),
  });
}

export function minutes(from?: string | null, to?: string | null): number {
  if (!from || !to) return 0;
  return Math.round((dateOf(to).getTime() - dateOf(from).getTime()) / 60000);
}

export function num(value: number | null | undefined, digits = 1): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits);
}

export function signed(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;
}

export function params(record: Record<string, unknown>): string {
  return Object.entries(record)
    .filter(([key]) => key !== 'wells')
    // 程序表（充放电工步、升温程序）只说几步；具体工步在批次页、流程编辑器里看
    .map(([key, value]) => (Array.isArray(value) ? `${key} 程序表 ${value.length} 步` : `${key} ${String(value)}`))
    .join(' · ');
}
