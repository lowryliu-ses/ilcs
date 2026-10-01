/* 曲线型结果（充放电曲线、谱图）的录入与查看。

   录入：从检测软件里复制两列（x、y）粘贴进来；带表头、单位行都行（不是数的行跳过）；有第三列就按它分成几条
   （如圈数）。也可以直接贴 JSON（{"x": [...], "y": [...]} 或 {"traces": [...]}）。点数上限、写法的最终校验在服务端。
   查看：列表里只有缩略，点开按需取完整的点，可以导出 CSV。 */
import { useMemo, useState } from 'react';

import { api } from './api';
import { CurveThumb, XYChart } from './chart';
import { useQuery } from './query';
import type { CurveTrace, SeriesDetail, SeriesView } from './types';
import { ListState, Modal, useToast } from './ui';

export type CurveValue = { x: number[]; y: number[] } | { traces: CurveTrace[] };

export type ParsedCurve = { value: CurveValue | null; traces: CurveTrace[]; points: number; skipped: number; problems: string[] };

function tracesOf(value: unknown): CurveTrace[] | null {
  if (Array.isArray(value)) {
    if (!value.every((row) => Array.isArray(row) && row.length === 2)) return null;
    return [{ name: '', x: value.map((row) => Number(row[0])), y: value.map((row) => Number(row[1])) }];
  }
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  const raw = Array.isArray(record.traces) ? record.traces : [record];
  const out: CurveTrace[] = [];
  for (const item of raw) {
    const trace = item as Record<string, unknown>;
    if (!Array.isArray(trace.x) || !Array.isArray(trace.y)) return null;
    out.push({ name: String(trace.name ?? ''), x: trace.x.map(Number), y: trace.y.map(Number) });
  }
  return out;
}

/** 粘贴的文字 → 曲线。返回解析出的曲线、点数、跳过的行数与问题（问题不为空就不该提交） */
export function parseCurveText(text: string): ParsedCurve {
  const trimmed = text.trim();
  const empty: ParsedCurve = { value: null, traces: [], points: 0, skipped: 0, problems: [] };
  if (!trimmed) return empty;
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      const traces = tracesOf(JSON.parse(trimmed));
      if (!traces) return { ...empty, problems: ['JSON 要写成 {"x": [...], "y": [...]}、{"traces": [...]} 或 [[x, y], ...]'] };
      const bad = traces.some((trace) => trace.x.length !== trace.y.length || [...trace.x, ...trace.y].some((v) => !Number.isFinite(v)));
      const points = traces.reduce((sum, trace) => sum + trace.x.length, 0);
      return {
        value: traces.length === 1 && !traces[0].name ? { x: traces[0].x, y: traces[0].y } : { traces },
        traces, points, skipped: 0,
        problems: bad ? ['有的曲线 x、y 长短不一或有不是数的点'] : points < 2 ? ['曲线至少要 2 个点'] : [],
      };
    } catch (caught) {
      return { ...empty, problems: [`JSON 无效：${caught instanceof Error ? caught.message : caught}`] };
    }
  }
  const groups = new Map<string, CurveTrace>();
  let skipped = 0;
  for (const line of trimmed.split(/\r?\n/)) {
    if (!line.trim()) continue;
    // 有 Tab、逗号、分号就按它们分列，否则按空白
    const parts = (/[\t,;]/.test(line) ? line.split(/[\t,;]/) : line.trim().split(/\s+/)).map((cell) => cell.trim());
    const x = Number(parts[0]);
    const y = Number(parts[1]);
    if (parts.length < 2 || parts[0] === '' || parts[1] === '' || !Number.isFinite(x) || !Number.isFinite(y)) {
      skipped += 1;
      continue;
    }
    const name = parts.length >= 3 ? parts[2] : '';
    const trace = groups.get(name) ?? { name, x: [], y: [] };
    trace.x.push(x);
    trace.y.push(y);
    groups.set(name, trace);
  }
  const traces = [...groups.values()];
  const points = traces.reduce((sum, trace) => sum + trace.x.length, 0);
  if (!points) return { ...empty, skipped, problems: ['没有读到成对的数：每行两列 x、y（Tab、逗号或空格分隔）'] };
  return {
    value: traces.length === 1 && !traces[0].name ? { x: traces[0].x, y: traces[0].y } : { traces },
    traces, points, skipped,
    problems: traces.some((trace) => trace.x.length < 2) ? ['每条曲线至少要 2 个点'] : [],
  };
}

/** 曲线 → 粘贴用的文字（修订时预填） */
export function curveText(traces: CurveTrace[]): string {
  const named = traces.length > 1 || traces.some((trace) => trace.name);
  return traces
    .flatMap((trace) => trace.x.map((x, index) => [x, trace.y[index], ...(named ? [trace.name] : [])].join('\t')))
    .join('\n');
}

/** 粘贴框：边贴边看解析出了几条、多少点，缩略图对一眼形状 */
export function CurveInput({
  value, onChange, xLabel, yLabel, disabled,
}: { value: string; onChange: (text: string) => void; xLabel?: string; yLabel?: string; disabled?: boolean }) {
  const parsed = useMemo(() => parseCurveText(value), [value]);
  return (
    <div className="curve-input">
      <textarea
        className="mono"
        rows={4}
        value={value}
        disabled={disabled}
        placeholder={`从检测软件复制两列粘贴：${xLabel || 'x'}<Tab>${yLabel || 'y'}；第三列是圈数之类就分成几条`}
        onChange={(event) => onChange(event.target.value)}
      />
      {value.trim() ? (
        <div className="row small">
          {parsed.traces.length ? <CurveThumb traces={parsed.traces} /> : null}
          <span className={parsed.problems.length ? 'bad-text' : 'muted'}>
            {parsed.problems.length
              ? parsed.problems[0]
              : `${parsed.traces.length > 1 ? `${parsed.traces.length} 条、` : ''}${parsed.points} 点${parsed.skipped ? `，跳过 ${parsed.skipped} 行不是数的行` : ''}`}
          </span>
        </div>
      ) : null}
    </div>
  );
}

function download(filename: string, text: string) {
  const blob = new Blob(['﻿' + text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/** 一条曲线结果的完整数据：图 + 概要 + 导出 CSV */
export function CurveDialog({ valueId, title, onClose }: { valueId: string; title: string; onClose: () => void }) {
  const toast = useToast();
  const detail = useQuery<SeriesDetail>(`result-series:${valueId}`, () => api.get<SeriesDetail>(`/result-values/${valueId}/series`));
  const [shown, setShown] = useState<string | null>(null);
  const data = detail.data;
  const xName = data ? `${data.x_label || 'x'}${data.x_unit ? `（${data.x_unit}）` : ''}` : 'x';
  const yName = data ? `${data.metric_name}${data.unit ? `（${data.unit}）` : ''}` : 'y';
  return (
    <Modal
      title={title}
      wide
      onClose={onClose}
      footer={
        <>
          {data ? (
            <button
              className="btn"
              onClick={() => {
                const header = ['trace', data.x_label || 'x', data.metric_name || 'y'].join(',');
                const lines = data.traces.flatMap((trace) => trace.x.map((x, index) => [JSON.stringify(trace.name), x, trace.y[index]].join(',')));
                download(`${data.metric_code}-v${data.result_version}.csv`, [header, ...lines].join('\n'));
                toast.push('已导出');
              }}
            >
              导出 CSV
            </button>
          ) : null}
          <button className="btn" onClick={onClose}>关闭</button>
        </>
      }
    >
      <ListState loading={detail.loading && !data} error={detail.error} />
      {data ? (
        <>
          <XYChart
            traces={data.traces.map((trace, index) => ({ key: `${index}`, label: trace.name || `第 ${index + 1} 条`, group: trace.name || `第 ${index + 1} 条`, x: trace.x, y: trace.y }))}
            xLabel={xName}
            yLabel={yName}
            highlight={shown}
            onPick={(key) => setShown(shown === key ? null : key)}
          />
          <div className="small muted">
            {data.traces.length} 条 · {data.points} 点
            {data.x_range ? ` · x ${data.x_range[0]}–${data.x_range[1]}` : ''}
            {data.y_range ? ` · y ${data.y_range[0]}–${data.y_range[1]}${data.unit ? ` ${data.unit}` : ''}` : ''}
            {data.traces.length > 1 ? ' · 点一条曲线突出显示它' : ''}
          </div>
        </>
      ) : null}
    </Modal>
  );
}

const QUALITY: Record<string, string> = { valid: '有效', suspect: '可疑', invalid: '无效', unassessed: '未判定' };
const REVIEW: Record<string, string> = { pending: '待复核', approved: '已通过', rejected: '已退回' };

/** 一个曲线指标的样本叠加：按条件组着色；点表格里的样本突出它；没纳入的列出原因（不悄悄少画） */
export function CurveOverlay({ url, compact = false }: { url: string; compact?: boolean }) {
  const view = useQuery<SeriesView>(`series-view:${url}`, () => api.get<SeriesView>(url));
  const [picked, setPicked] = useState<string | null>(null);
  const [opened, setOpened] = useState<{ id: string; title: string } | null>(null);
  const data = view.data;
  if (!data) return <ListState loading={view.loading} error={view.error} />;
  const traces = data.samples.flatMap((sample) =>
    sample.traces.map((trace, index) => ({
      key: `${sample.result_value_id}:${index}`,
      label: `${sample.assignment_id}${trace.name ? ` · ${trace.name}` : ''}（${sample.condition_label || sample.condition_group || '未分组'}）`,
      group: sample.condition_label || sample.condition_group || '未分组',
      x: trace.x,
      y: trace.y,
    })),
  );
  const xName = `${data.x_label || 'x'}${data.x_unit ? `（${data.x_unit}）` : ''}`;
  const yName = `${data.metric_name}${data.unit ? `（${data.unit}）` : ''}`;
  return (
    <div className="stack">
      {traces.length ? (
        <XYChart traces={traces} xLabel={xName} yLabel={yName} width={1000} height={compact ? 240 : 320}
          highlight={picked ? traces.find((trace) => trace.key.startsWith(`${picked}:`))?.key ?? null : null} />
      ) : (
        <div className="small muted">当前范围内没有可画的曲线{data.official ? '：正式范围只画审核通过且质量有效的' : ''}</div>
      )}
      {!compact && data.samples.length ? (
        <table className="compact">
          <thead>
            <tr>
              <th>样本</th>
              <th>条件</th>
              <th>曲线</th>
              <th>质量 / 审核</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.samples.map((sample) => (
              <tr key={sample.result_value_id} className={picked === sample.result_value_id ? 'selected' : undefined}
                onMouseEnter={() => setPicked(sample.result_value_id)} onMouseLeave={() => setPicked(null)}>
                <td className="mono small">{sample.assignment_id}{sample.well ? <span className="tiny muted"> · {sample.well}</span> : null}</td>
                <td className="small">{sample.condition_label || sample.condition_group}{sample.is_control ? ' · 对照' : ''}</td>
                <td className="small">{sample.traces.length > 1 ? `${sample.traces.length} 条 · ` : ''}{sample.points} 点 · v{sample.result_version}</td>
                <td className="small">{QUALITY[sample.quality] ?? sample.quality} / {REVIEW[sample.review_state] ?? sample.review_state}</td>
                <td className="row-end">
                  <button className="btn sm" onClick={() => setOpened({ id: sample.result_value_id, title: `${data.metric_name} · ${sample.assignment_id} · v${sample.result_version}` })}>
                    完整曲线
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {data.excluded.length ? (
        <div className="tiny muted">
          没纳入 {data.excluded.length} 条：{data.excluded.slice(0, 6).map((row) => `${row.assignment_id}（${row.reason_label}）`).join('、')}
          {data.excluded.length > 6 ? ' …' : ''}
        </div>
      ) : null}
      {opened ? <CurveDialog valueId={opened.id} title={opened.title} onClose={() => setOpened(null)} /> : null}
    </div>
  );
}
