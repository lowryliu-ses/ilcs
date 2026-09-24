import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api, pageQuery } from '../../shared/api';
import { LineChart } from '../../shared/chart';
import { num, signed } from '../../shared/format';
import { useQuery } from '../../shared/query';
import type { Analysis, AnalysisView, MetricBlock } from '../../shared/types';
import { Empty, ListState, Panel, Pill, ScopeNotice, useToast } from '../../shared/ui';

type ResultIndexRow = {
  batch_id: string;
  recipe_id: string;
  recipe_name: string;
  state: string;
  plan_id: string;
  sample_done: number;
  sample_count: number;
  typed_results: boolean;
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
                    {row.typed_results ? row.result_count : <span className="tag">历史三指标</span>}
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

  // 历史批次没有类型化结果：回落到旧的固定三指标视图，并明确标注
  if (view.legacy) {
    return <LegacyAnalysis batchId={batchId} view={view as unknown as Analysis} />;
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
                <option key={row.id} value={row.id} disabled={!row.numeric}>
                  {row.name}
                  {row.numeric ? '' : '（非数值，不进统计）'}
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

      {view.metrics.length ? (
        view.metrics.map((block) => (
          <MetricPanel key={block.metric_id} block={block} showEffects={view.show_factor_effects} />
        ))
      ) : (
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
          <span className="metric-label">最佳条件组</span>
          <strong className="metric-value">{summary.best_group ?? '—'}</strong>
          <span className="metric-hint">
            {summary.best_mean === null ? '无' : num(summary.best_mean, 2)}
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

/** 历史固定三指标视图。它按样本上的旧质量标记聚合，所以要标明这不是新模型的审核结论。 */
function LegacyAnalysis({ batchId, view }: { batchId: string; view: Analysis }) {
  const toast = useToast();
  return (
    <Panel
      title={`历史结果 · ${batchId}`}
      aside={
        <button
          className="btn sm"
          onClick={() =>
            api
              .download(`/results/${batchId}/export`, `${batchId}-results.csv`)
              .catch((error) => toast.push(error.message))
          }
        >
          导出 CSV
        </button>
      }
    >
      <div className="note warn">
        该批次没有类型化结果，显示的是历史固定三指标视图。表中的「有效」是历史人工质量标记，
        不等于新模型下的审核通过 + 质量有效，不能作为正式统计依据。
      </div>
      <div className="metrics">
        <div className="metric">
          <span className="metric-label">历史标记有效样品</span>
          <strong className="metric-value">
            {view.summary.valid_samples}/{view.summary.total_samples}
          </strong>
          <span className="metric-hint">历史质量标记</span>
        </div>
        <div className="metric">
          <span className="metric-label">中位 CV</span>
          <strong className="metric-value">{num(view.summary.median_cv_pct, 2)}%</strong>
          <span className="metric-hint">{view.summary.high_cv_groups} 个组 CV &gt; 3%</span>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>条件组</th>
            <th>条件</th>
            <th className="num">有效 / 总数</th>
            <th className="num">均值</th>
            <th className="num">CV%</th>
          </tr>
        </thead>
        <tbody>
          {view.groups.map((group) => (
            <tr key={group.group}>
              <td className="mono">{group.group}</td>
              <td className="small">{group.label}</td>
              <td className="num mono">
                {group.n_valid}/{group.n_total}
              </td>
              <td className="num mono">{num(group.mean, 2)}</td>
              <td className="num mono">{num(group.cv_pct, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}
