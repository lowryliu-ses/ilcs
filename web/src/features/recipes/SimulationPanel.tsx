/* 执行前仿真：方法在所有分支走法下能不能走完、最长多久、回环最坏拖多久；
   给定并发批次数时，在当前时间线上排一遍，看等待、跨度、最忙工位与物料是否够用。
   结论存在方法上并带内容指纹：步骤或 BOM 改过之后，「已验证」标记自动失效。
   提交评审与批准时服务端会在空时间线上重跑可执行性检查，有阻断项就拒绝。 */
import { useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation } from '../../shared/query';
import type { RecipeDetail, SimulationResult } from '../../shared/types';
import { CheckList, Empty, Panel, Pill, useToast } from '../../shared/ui';

export function SimulationBadge({ recipe }: { recipe: RecipeDetail }) {
  const sim = recipe.simulation as SimulationResult;
  if (!sim?.content_hash) return <Pill state="neutral" label="未仿真" />;
  if (!recipe.simulation_current) return <Pill state="scheduled" label="仿真已过期" />;
  return sim.ok ? <Pill state="running" label="仿真已验证" /> : <Pill state="fault" label="仿真未通过" />;
}

export function SimulationPanel({ recipe, canRun }: { recipe: RecipeDetail; canRun: boolean }) {
  const toast = useToast();
  const [concurrency, setConcurrency] = useState(1);
  const [latest, setLatest] = useState<SimulationResult | null>(null);
  const run = useMutation(
    () => api.post<SimulationResult>(`/recipes/${recipe.id}/simulate`, { concurrency, use_timeline: true }),
    { invalidates: [`recipes:${recipe.id}`], onSuccess: (result) => {
      setLatest(result);
      toast.push(result.ok ? '仿真通过' : '仿真发现阻断项');
    } },
  );
  const sim = (latest ?? recipe.simulation) as SimulationResult;
  const has = Boolean(sim?.content_hash);
  const loads = has ? Object.entries(sim.station_load_min ?? {}) : [];
  const busiest = Math.max(1, ...loads.map(([, minutes]) => minutes));

  return (
    <Panel
      title="执行前仿真"
      aside={
        <div className="row">
          <SimulationBadge recipe={recipe} />
          {canRun ? (
            <>
              <label className="small muted">
                并发批次{' '}
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={concurrency}
                  style={{ width: 56 }}
                  onChange={(event) => setConcurrency(Math.min(20, Math.max(1, Number(event.target.value) || 1)))}
                />
              </label>
              <button className="btn sm primary" disabled={run.pending} onClick={() => run.run().catch((error) => toast.push(error.message))}>
                {run.pending ? '仿真中…' : '运行仿真'}
              </button>
            </>
          ) : null}
        </div>
      }
    >
      {!has ? (
        <Empty>还没有仿真。运行一次看看所有分支走法、回环最坏情况、当前时间线上的等待与物料是否够用。</Empty>
      ) : (
        <>
          <div className="small muted">
            {clock(sim.at)} · v{sim.version} · 并发 {sim.concurrency} 批 · 阻断 {sim.summary.blocked} / 提醒 {sim.summary.warn} / 通过 {sim.summary.pass}
            {!recipe.simulation_current && !latest ? ' · 方法内容已改动，结论仅供参考，请重跑' : ''}
          </div>
          <CheckList checks={sim.checks} />
          <div className="grid cols-2">
            <div>
              <h4>分支走法（{sim.paths.length}）</h4>
              {sim.paths.length ? (
                <table>
                  <thead>
                    <tr>
                      <th>出口选择</th>
                      <th>步骤</th>
                      <th>关键路径</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sim.paths.map((path, index) => (
                      <tr key={index}>
                        <td className="small">{path.choices.length ? path.choices.join('；') : '无分支'}</td>
                        <td className="small mono">{path.steps}</td>
                        <td className="small mono">{path.duration_min} min</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty>—</Empty>
              )}
              {sim.loops.length ? (
                <>
                  <h4>回环最坏情况</h4>
                  <table>
                    <tbody>
                      {sim.loops.map((row) => (
                        <tr key={`${row.branch_step_id}:${row.case}`}>
                          <td className="small">
                            {row.label} <span className="tiny muted mono">{row.branch_step_id}</span>
                          </td>
                          <td className="small">回 {row.body_steps.join('、')}，最多 {row.max_loops} 次</td>
                          <td className="small mono">+{row.extra_min_worst} min</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              ) : null}
            </div>
            <div>
              <h4>工位占用（{sim.concurrency} 批合计）</h4>
              {loads.length ? (
                loads.map(([station, minutes]) => (
                  <div key={station} className="small" style={{ marginBottom: 6 }}>
                    <div className="row" style={{ justifyContent: 'space-between' }}>
                      <span className="mono">{station}</span>
                      <span className="mono">{minutes} min</span>
                    </div>
                    <div className="bar">
                      <span style={{ width: `${(minutes / busiest) * 100}%` }} />
                    </div>
                  </div>
                ))
              ) : (
                <Empty>没有排上工位</Empty>
              )}
            </div>
          </div>
        </>
      )}
    </Panel>
  );
}
