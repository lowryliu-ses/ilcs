import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api, pageQuery } from '../../shared/api';
import { LineChart } from '../../shared/chart';
import { CurveOverlay } from '../../shared/curves';
import { num, signed } from '../../shared/format';
import { useQuery } from '../../shared/query';
import type { AnalysisView, MetricBlock } from '../../shared/types';
import { Empty, ListState, Panel, Pill, ScopeNotice, useToast } from '../../shared/ui';

type ResultIndexRow = {
  batch_id: string;
  recipe_id: string;
  recipe_name: string;
  state: string;
  plan_id: string;
  sample_done: number;
  sample_count: number;
  result_count: number;
  pending_review: number;
  official_count: number;
  is_golden: boolean;
};

export function ResultsPage() {
  const { batchId } = useParams();
  const navigate = useNavigate();
  const index = useQuery<ResultIndexRow[]>('results', () => api.get<ResultIndexRow[]>('/results'));

  const rows = index.data ?? [];
  const current = batchId ?? rows[0]?.batch_id;

  return (
    <div className="page">
      <div className="page-head">
        <h1>结果分析</h1>
        <span className="small muted">
          正式统计要求审核通过、质量有效、且是选定的结果版本。探索性范围可以放宽，但会明确标注，
          不能直接用于正式报告。
        </span>
      </div>

      <Panel title={`有结果的批次（${rows.length}）`} flush>
        <ListState
          loading={index.loading && !index.data}
          error={index.error}
          empty={!rows.length}
          emptyText="还没有回传结果"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>批次</th>
                <th>流程</th>
                <th>运行分配</th>
                <th>结果明细</th>
                <th>待复核</th>
                <th>可纳入正式统计</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.batch_id}
                  className={`clickable${row.batch_id === current ? ' selected' : ''}`}
                  onClick={() => navigate(`/results/${row.batch_id}`)}
                >
                  <td className="mono">
                    {row.batch_id}
                    {row.is_golden ? <span className="tag"> 黄金批次</span> : null}
                  </td>
                  <td className="small">{row.recipe_name}</td>
                  <td className="mono small">
                    {row.sample_done}/{row.sample_count}
                  </td>
                  <td className="mono small">
                    {row.result_count}
                  </td>
                  <td className="mono small">
                    {row.pending_review ? <b className="warn-text">{row.pending_review}</b> : 0}
                  </td>
                  <td className="mono small">{row.official_count}</td>
                  <td>
                    <Pill state={row.state} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      {current ? <BatchAnalysis batchId={current} /> : null}
    </div>
  );
}

function BatchAnalysis({ batchId }: { batchId: string }) {
  const toast = useToast();
  const [official, setOfficial] = useState(true);
  const [selected, setSelected] = useState<string[]>([]);

  const query = pageQuery({ official, metric_ids: selected.join(',') });
  const analysis = useQuery<AnalysisView>(
    `results:${batchId}:${query}`, () => api.get<AnalysisView>(`/results/${batchId}${query}`),
  );

  const view = analysis.data;

  if (!view) {
    return (
      <Panel title="分析">
        <ListState loading={analysis.loading} error={analysis.error} />
      </Panel>
    );
  }

  return (
    <>
      <Panel
        title={`分析 · ${batchId}`}
        aside={
          <div className="filters">
            <label className="small">
              <input
                type="checkbox"
                checked={!official}
                onChange={(event) => setOfficial(!event.target.checked)}
              />
              探索性范围
            </label>
            <select
              multiple
              size={3}
              value={selected}
              onChange={(event) =>
                setSelected(Array.from(event.target.selectedOptions).map((option) => option.value))
              }
            >
              {view.available_metrics.map((row) => (
                <option key={row.id} value={row.id} disabled={!row.numeric && row.value_type !== 'series'}>
                  {row.name}
                  {row.numeric ? '' : row.value_type === 'series' ? '（曲线，叠加画）' : '（非数值，不进统计）'}
                </option>
              ))}
            </select>
            <button
              className="btn sm"
              onClick={() =>
                api
                  .download(
                    `/results/${batchId}/export${pageQuery({ official })}`,
                    `${batchId}-results-${official ? 'official' : 'exploratory'}.csv`,
                  )
                  .catch((error) => toast.push(error.message))
              }
            >
              导出 CSV
            </button>
          </div>
        }
      >
        <ScopeNotice official={view.official} label={view.scope_label} />
        <div className="small muted">
          方案 {view.plan_id}（{view.plan_type}）· 流程 {view.recipe_name}
          {view.show_factor_effects ? '' : ' · 非矩阵实验不显示因子主效应'}
        </div>
        {view.non_numeric_metrics.length ? (
          <div className="note">
            非数值指标不进入数值统计，请在数据审核页逐条查看：
            {view.non_numeric_metrics.map((row) => row.metric_name).join('、')}
          </div>
        ) : null}
      </Panel>

      {(view.series_metrics ?? []).map((row) => (
        <Panel key={row.metric_id} title={`曲线 · ${row.metric_name}`}
          aside={<span className="small muted">按样本叠加，同一条件组同色；不进数值统计</span>}>
          <CurveOverlay url={`/results/${batchId}/series${pageQuery({ metric_id: row.metric_id, official })}`} />
        </Panel>
      ))}

      {view.metrics.length ? (
        view.metrics.map((block) => (
          <MetricPanel key={block.metric_id} block={block} showEffects={view.show_factor_effects} />
        ))
      ) : (view.series_metrics ?? []).length ? null : (
        <Panel title="统计">
          <Empty>
            当前范围内没有可统计的数值结果
            {official ? '：正式统计要求审核通过且质量有效' : ''}
          </Empty>
        </Panel>
      )}
    </>
  );
}

function MetricPanel({ block, showEffects }: { block: MetricBlock; showEffects: boolean }) {
  const summary = block.summary;
  // 各组均值完全一样时没有高低可言：不点名「最高」「最低」哪一组
  const tied = summary.lowest_group !== null && summary.highest_mean === summary.lowest_mean;
  // 条件组均值按组序排成一条折线；空组保留为 null，图上断开而不是补 0
  const series = useMemo(
    () => [
      {
        key: block.metric_id,
        label: `${block.metric_name} 组均值`,
        values: block.groups.map((group) => group.mean),
        variant: 'measured' as const,
      },
    ],
    [block.groups, block.metric_id, block.metric_name],
  );

  return (
    <Panel title={`${block.metric_name}${block.unit ? `（${block.unit}）` : ''}`}>
      <div className="metrics">
        <div className="metric">
          <span className="metric-label">纳入 / 排除</span>
          <strong className="metric-value">
            {summary.included} / {summary.excluded}
          </strong>
          <span className="metric-hint">
            {summary.exclusions.map((row) => `${row.label} ${row.count}`).join('；') || '无排除'}
          </span>
        </div>
        <div className="metric">
          <span className="metric-label">均值</span>
          <strong className="metric-value">{num(summary.mean, 2)}</strong>
          <span className="metric-hint">
            SD {num(summary.sd, 3)} · CV {num(summary.cv_pct, 2)}%
          </span>
        </div>
        <div className="metric">
          {/* 只按数值高低列出：越大越好、越小越好还是越接近目标越好由指标与方案决定，这里不判优劣 */}
          <span className="metric-label">均值最高 / 最低的条件组</span>
          <strong className="metric-value">
            {tied ? '各组均值相同' : summary.highest_group ?? '—'}
            {!tied && summary.lowest_group ? ` / ${summary.lowest_group}` : ''}
          </strong>
          <span className="metric-hint">
            {summary.highest_mean === null ? '无' : num(summary.highest_mean, 2)}
            {!tied && summary.lowest_mean !== null ? ` / ${num(summary.lowest_mean, 2)}` : ''}
            {' · 只按高低列出，不判优劣'}
            {summary.single_repeat ? ' · 单次重复，无法给出组内 CV' : ''}
          </span>
        </div>
      </div>

      {block.groups.length > 1 ? (
        <LineChart
          series={series}
          unit={block.unit}
          xLabels={[block.groups[0].group, block.groups[block.groups.length - 1].group]}
          caption={`按条件组的均值；只含纳入正式统计的 ${summary.included} 条记录`}
        />
      ) : null}

      <table>
        <thead>
          <tr>
            <th>条件组</th>
            <th>条件</th>
            <th className="num">纳入</th>
            <th className="num">排除</th>
            <th className="num">均值</th>
            <th className="num">SD</th>
            <th className="num">CV%</th>
          </tr>
        </thead>
        <tbody>
          {block.groups.map((group) => (
            <tr key={group.group}>
              <td className="mono">
                {group.group}
                {group.is_control ? <span className="tag"> 对照</span> : null}
              </td>
              <td className="small">{group.label}</td>
              <td className="num mono">{group.n_included}</td>
              <td className="num mono">{group.n_excluded || ''}</td>
              <td className="num mono">{num(group.mean, 2)}</td>
              <td className="num mono">{num(group.sd, 3)}</td>
              <td className="num mono">{num(group.cv_pct, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {block.excluded.length ? (
        <>
          <div className="panel-body small">
            <b>被排除记录</b>
            <span className="muted"> · 它们不进入正式结论统计，但会出现在报告的排除说明里</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>样本</th>
                <th>检测任务</th>
                <th>版本</th>
                <th>质量</th>
                <th>审核</th>
                <th>排除原因</th>
              </tr>
            </thead>
            <tbody>
              {block.excluded.map((row, index) => (
                <tr key={`${row.assignment_id}-${index}`}>
                  <td className="mono small">{row.assignment_id}</td>
                  <td className="mono tiny">{row.analysis_task_id.slice(0, 8)}</td>
                  <td className="small">v{row.result_version}</td>
                  <td>
                    <Pill state={row.quality} />
                  </td>
                  <td>
                    <Pill state={row.review_state} />
                  </td>
                  <td className="small">{row.reason_label}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}

      {showEffects && block.effects.length ? (
        <>
          <div className="panel-body small">
            <b>因子主效应</b>
            <span className="muted"> · 只在矩阵实验里有意义</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>因子</th>
                <th>各水平均值</th>
                <th className="num">极差</th>
              </tr>
            </thead>
            <tbody>
              {block.effects.map((effect) => (
                <tr key={effect.factor}>
                  <td>{effect.factor}</td>
                  <td className="small mono">
                    {effect.levels
                      .map((level) => `${String(level.level)}${effect.unit}→${num(level.mean, 2)}（n=${level.n}）`)
                      .join('  ')}
                  </td>
                  <td className="num mono">{signed(effect.range, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </Panel>
  );
}
