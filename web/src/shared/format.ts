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

export function params(record: Record<string, number | string>): string {
  return Object.entries(record)
    .map(([key, value]) => `${key} ${value}`)
    .join(' · ');
}
