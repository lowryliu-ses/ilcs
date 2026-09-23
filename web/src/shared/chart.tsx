/* 图表原子组件。内联 SVG，颜色一律走 index.css 的设计令牌类，不在 JS 里拼色值。

   刻意不引第三方图表库：这里只有折线与点图两种形态，自绘比适配主题与无障碍更省事；
   将来遥测点数上量（TimescaleDB 连续聚合）再换 uPlot，只需替换本文件。 */
import type { ReactNode } from 'react';

const PAD = { top: 12, right: 12, bottom: 26, left: 46 };
/* 序列短于这个数就同时画点：折线在稀疏数据上几乎不可见。 */
const SPARSE_POINTS = 24;

type Domain = { lo: number; hi: number };

function domainOf(values: (number | null | undefined)[], padding = 0.08): Domain {
  const real = values.filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  if (!real.length) return { lo: 0, hi: 1 };
  let lo = Math.min(...real);
  let hi = Math.max(...real);
  if (lo === hi) {
    const span = Math.abs(lo) || 1;
    lo -= span * 0.1;
    hi += span * 0.1;
  } else {
    const span = hi - lo;
    lo -= span * padding;
    hi += span * padding;
  }
  return { lo, hi };
}

function ticks(domain: Domain, count = 4): number[] {
  const step = (domain.hi - domain.lo) / count;
  return Array.from({ length: count + 1 }, (_, index) => domain.lo + step * index);
}

function label(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1000) return value.toFixed(0);
  if (abs >= 10) return value.toFixed(1);
  if (abs >= 1) return value.toFixed(2);
  return value.toPrecision(2);
}

/* ---------- 折线：遥测 ---------- */

export type LineSeries = {
  key: string;
  label: string;
  values: (number | null)[];
  variant?: 'measured' | 'golden';
};

export function LineChart({
  series,
  reference,
  referenceLabel,
  unit = '',
  height = 170,
  width = 720,
  xLabels,
  caption,
}: {
  series: LineSeries[];
  reference?: number | null;
  referenceLabel?: string;
  unit?: string;
  height?: number;
  width?: number;
  xLabels?: [string, string];
  caption?: ReactNode;
}) {
  const all = series.flatMap((s) => s.values).concat(typeof reference === 'number' ? [reference] : []);
  const domain = domainOf(all);
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const longest = Math.max(1, ...series.map((s) => s.values.length));
  const x = (index: number) => PAD.left + (longest === 1 ? plotW / 2 : (plotW * index) / (longest - 1));
  const y = (value: number) => PAD.top + plotH * (1 - (value - domain.lo) / (domain.hi - domain.lo));

  const path = (values: (number | null)[]) => {
    let out = '';
    let penDown = false;
    values.forEach((value, index) => {
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        penDown = false;
        return;
      }
      out += `${penDown ? 'L' : 'M'}${x(index).toFixed(1)},${y(value).toFixed(1)} `;
      penDown = true;
    });
    return out.trim();
  };

  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" preserveAspectRatio="xMidYMid meet">
        {ticks(domain).map((value) => (
          <g key={value}>
            <line className="chart-grid" x1={PAD.left} x2={width - PAD.right} y1={y(value)} y2={y(value)} />
            <text className="chart-tick" x={PAD.left - 6} y={y(value) + 3} textAnchor="end">
              {label(value)}
            </text>
          </g>
        ))}
        {typeof reference === 'number' ? (
          <line className="chart-reference" x1={PAD.left} x2={width - PAD.right} y1={y(reference)} y2={y(reference)} />
        ) : null}
        {series.map((s) => (
          <path key={s.key} className={`chart-line ${s.variant ?? 'measured'}`} d={path(s.values)} />
        ))}
        {/* 稀疏序列（例如每步只回传一个点）光靠折线看不见，补上点标记。 */}
        {series
          .filter((s) => s.values.length <= SPARSE_POINTS)
          .flatMap((s) =>
            s.values.map((value, index) =>
              typeof value === 'number' && Number.isFinite(value) ? (
                <circle
                  key={`${s.key}-${index}`}
                  className={`chart-dot ${s.variant ?? 'measured'}`}
                  cx={x(index)}
                  cy={y(value)}
                  r={2.6}
                />
              ) : null,
            ),
          )}
        {xLabels ? (
          <>
            <text className="chart-tick" x={PAD.left} y={height - 8}>
              {xLabels[0]}
            </text>
            <text className="chart-tick" x={width - PAD.right} y={height - 8} textAnchor="end">
              {xLabels[1]}
            </text>
          </>
        ) : null}
      </svg>
      <figcaption className="chart-legend">
        {series.map((s) => (
          <span key={s.key}>
            <i className={`swatch line ${s.variant ?? 'measured'}`} />
            {s.label}
          </span>
        ))}
        {typeof reference === 'number' ? (
          <span>
            <i className="swatch line reference" />
            {referenceLabel ?? '设定值'} {label(reference)}
            {unit}
          </span>
        ) : null}
        {caption}
      </figcaption>
    </figure>
  );
}

/* ---------- 点图：结果按条件组分布 ---------- */

export type ScatterGroup = {
  key: string;
  label: string;
  is_control?: boolean;
  points: { key: string; value: number | null; quality: string | null; title?: string }[];
  mean: number | null;
  golden_mean: number | null;
};

export function ScatterChart({
  groups,
  unit = '',
  height = 230,
  width = 720,
  onPick,
}: {
  groups: ScatterGroup[];
  unit?: string;
  height?: number;
  width?: number;
  onPick?: (groupKey: string, pointKey: string) => void;
}) {
  const values = groups.flatMap((g) => [...g.points.map((p) => p.value), g.mean, g.golden_mean]);
  const domain = domainOf(values, 0.12);
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const slot = plotW / Math.max(1, groups.length);
  const cx = (index: number) => PAD.left + slot * (index + 0.5);
  const y = (value: number) => PAD.top + plotH * (1 - (value - domain.lo) / (domain.hi - domain.lo));

  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" preserveAspectRatio="xMidYMid meet">
        {ticks(domain).map((value) => (
          <g key={value}>
            <line className="chart-grid" x1={PAD.left} x2={width - PAD.right} y1={y(value)} y2={y(value)} />
            <text className="chart-tick" x={PAD.left - 6} y={y(value) + 3} textAnchor="end">
              {label(value)}
            </text>
          </g>
        ))}
        {groups.map((group, index) => {
          const spread = Math.min(9, slot / Math.max(2, group.points.length + 1));
          return (
            <g key={group.key}>
              {typeof group.mean === 'number' ? (
                <line
                  className="chart-mean"
                  x1={cx(index) - slot * 0.28}
                  x2={cx(index) + slot * 0.28}
                  y1={y(group.mean)}
                  y2={y(group.mean)}
                />
              ) : null}
              {typeof group.golden_mean === 'number' ? (
                <circle className="chart-golden" cx={cx(index)} cy={y(group.golden_mean)} r={6} />
              ) : null}
              {group.points.map((point, order) => {
                if (typeof point.value !== 'number') return null;
                const offset = (order - (group.points.length - 1) / 2) * spread;
                return (
                  <circle
                    key={point.key}
                    className={`chart-point ${point.quality ?? 'pending'}${onPick ? ' pickable' : ''}`}
                    cx={cx(index) + offset}
                    cy={y(point.value)}
                    r={3.6}
                    onClick={onPick ? () => onPick(group.key, point.key) : undefined}
                  >
                    <title>{point.title ?? `${point.key}：${label(point.value)}${unit}`}</title>
                  </circle>
                );
              })}
              <text className="chart-tick" x={cx(index)} y={height - 8} textAnchor="middle">
                {group.label}
                {group.is_control ? ' ·对照' : ''}
              </text>
            </g>
          );
        })}
      </svg>
      <figcaption className="chart-legend">
        <span>
          <i className="swatch dot valid" />
          有效
        </span>
        <span>
          <i className="swatch dot suspect" />
          可疑
        </span>
        <span>
          <i className="swatch dot invalid" />
          无效
        </span>
        <span>
          <i className="swatch line mean" />
          组均值（仅计有效样品）
        </span>
        <span>
          <i className="swatch ring" />
          黄金批次同组均值
        </span>
      </figcaption>
    </figure>
  );
}
