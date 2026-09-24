import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useQuery } from '../../shared/query';
import type { Dashboard, KpiReport } from '../../shared/types';
import { Empty, GateBanner, Metric, Panel, Pill, Severity } from '../../shared/ui';

export function DashboardPage() {
  const { data, loading } = useQuery<Dashboard>('dashboard', () => api.get<Dashboard>('/dashboard'), 15000);

  if (!data) return <div className="boot">{loading ? '加载中…' : '无数据'}</div>;
  const { counts } = data;

  const exportHandover = async () => {
    const summary = await api.get<Record<string, unknown>>('/handover');
    const blob = new Blob([JSON.stringify(summary, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = Object.assign(document.createElement('a'), { href: url, download: `handover-${Date.now()}.json` });
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>工作台</h1>
        <div className="row">
          <span className="small muted">数据时间 {clock(data.now)}</span>
          <button className="btn sm" onClick={exportHandover}>
            导出交班摘要
          </button>
        </div>
      </div>
      <GateBanner gate={data.gate} />

      <div className="metrics">
        <Metric
          label="我的任务"
          value={counts.my_tasks}
          hint={`待接单 ${counts.pending_accept} · 人工待办 ${counts.manual_steps}`}
        />
        <Metric
          label="待复核"
          value={counts.pending_result_reviews}
          hint={`流程审核 ${counts.step_reviews} · 报告审核 ${counts.report_reviews}`}
        />
        <Metric label="活动批次" value={counts.active_batches} hint={`运行 ${counts.running} · 保持 ${counts.held}`} />
        <Metric label="未确认报警" value={counts.open_alarms} hint={`结果未知指令 ${counts.unknown_commands}`} />
        <Metric
          label="过期项"
          value={counts.expiring_qualifications + counts.expiring_lots + counts.unavailable_assets}
          hint={`资质 ${counts.expiring_qualifications} · 物料 ${counts.expiring_lots} · 设备 ${counts.unavailable_assets}`}
        />
        <Metric
          label="今日可用结果"
          value={counts.official_results_today}
          hint="审核通过 + 质量有效，才算可用"
        />
      </div>

      <KpiSection />

      <div className="grid cols-2">
        <Panel title={`我的任务（${data.my_tasks.length}）`} flush>
          {data.my_tasks.length || data.pending_accept.length ? (
            <table>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>方案</th>
                  <th>状态</th>
                  <th>下一动作</th>
                </tr>
              </thead>
              <tbody>
                {[...data.my_tasks, ...data.pending_accept].map((task) => (
                  <tr key={task.id}>
                    <td className="mono">
                      {task.id}
                      {task.overdue ? <div className="tiny bad-text">已过期</div> : null}
                    </td>
                    <td className="small">
                      <Link to={`/plans/${task.plan_id}`}>{task.plan_id}</Link>
                    </td>
                    <td>
                      <Pill state={task.state} label={task.state_label} />
                    </td>
                    <td className="small">
                      {task.state === 'pending_accept'
                        ? '去任务中心接单'
                        : task.batch_id
                        ? <Link to={`/batches/${task.batch_id}`}>打开批次 {task.batch_id}</Link>
                        : '建立批次'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>没有分配给你的任务</Empty>
          )}
        </Panel>

        <Panel title="待我处理的节点与数据" flush>
          {data.manual_todos.length || data.review_todos.length || data.pending_result_reviews.length ? (
            <table>
              <tbody>
                {data.manual_todos.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <span className={`kind ${run.kind}`}>{run.kind_label}</span>{' '}
                      <Link to={`/batches/${run.batch_id}`}>{run.step_name}</Link>
                      <div className="tiny muted mono">{run.batch_id}</div>
                    </td>
                    <td className="small muted">填写人工记录</td>
                  </tr>
                ))}
                {data.review_todos.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <span className="kind review">审核</span>{' '}
                      <Link to={`/batches/${run.batch_id}`}>{run.step_name}</Link>
                      <div className="tiny muted mono">{run.batch_id}</div>
                    </td>
                    <td className="small muted">流程审核</td>
                  </tr>
                ))}
                {data.pending_result_reviews.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <Link to="/data-review">结果 v{row.result_version}</Link>
                      <div className="tiny muted mono">任务 {row.analysis_task_id.slice(0, 8)}</div>
                    </td>
                    <td className="small muted">数据复核</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>没有待你处理的节点</Empty>
          )}
        </Panel>
      </div>

      <div className="grid cols-2">
        <Panel title="待人工处理" flush>
          {data.todo.length ? (
            <table>
              <thead>
                <tr>
                  <th>批次</th>
                  <th>动作</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {data.todo.map((item) => (
                  <tr key={item.batch_id}>
                    <td>
                      <Link to={`/batches/${item.batch_id}`} className="mono">
                        {item.batch_id}
                      </Link>
                    </td>
                    <td>
                      <b>{item.what}</b>
                      <div className="tiny muted">{item.who}</div>
                    </td>
                    <td className="small muted">{item.why}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>没有待人工处理的事项</Empty>
          )}
        </Panel>

        <Panel title="硬时限窗口" flush>
          {data.hard_windows.length ? (
            <table>
              <thead>
                <tr>
                  <th>批次</th>
                  <th>下一步</th>
                  <th className="num">剩余</th>
                </tr>
              </thead>
              <tbody>
                {data.hard_windows.map((row) => (
                  <tr key={`${row.batch_id}-${row.step_name}`}>
                    <td className="mono">{row.batch_id}</td>
                    <td>
                      {row.step_name}
                      <div className="tiny muted">上限 {row.max_gap_min} min · 截止 {clock(row.deadline)}</div>
                    </td>
                    <td className={`num ${row.remaining_min < 0 ? 'bad-text' : ''}`}>{row.remaining_min} min</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>当前没有在算的硬时限</Empty>
          )}
        </Panel>
      </div>

      <Panel title="活动批次" flush>
        {data.active_batches.length ? (
          <table>
            <thead>
              <tr>
                <th>批次</th>
                <th>流程</th>
                <th>状态</th>
                <th>当前工位</th>
                <th>进度</th>
                <th>计划时间</th>
              </tr>
            </thead>
            <tbody>
              {data.active_batches.map((batch) => (
                <tr key={batch.id}>
                  <td>
                    <Link to={`/batches/${batch.id}`} className="mono">
                      {batch.id}
                    </Link>
                  </td>
                  <td>
                    {batch.recipe_name}
                    <div className="tiny muted">
                      {batch.recipe_id} v{batch.version} · 计划 {batch.plan_id}
                    </div>
                  </td>
                  <td>
                    <Pill state={batch.state} label={batch.state_label} />
                  </td>
                  <td className="mono">{batch.current_station ?? '—'}</td>
                  <td>
                    第 {Math.min(batch.current_step + 1, batch.step_count)}/{batch.step_count} 步
                    <div className="tiny muted">
                      样品 {batch.sample_done}/{batch.sample_count}
                    </div>
                  </td>
                  <td className="small mono">
                    {clock(batch.starts_at)} → {clock(batch.ends_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>没有活动批次</Empty>
        )}
      </Panel>

      <div className="grid cols-2">
        <Panel title="报警" flush>
          {data.alarms.length ? (
            <table>
              <thead>
                <tr>
                  <th>级别</th>
                  <th>来源</th>
                  <th>内容</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {data.alarms.map((alarm) => (
                  <tr key={alarm.id}>
                    <td>
                      <Severity level={alarm.severity} /> {alarm.severity_label}
                    </td>
                    <td className="mono small">{alarm.source_id}</td>
                    <td className="small">{alarm.message}</td>
                    <td>
                      <Pill state={alarm.state} label={alarm.state_label} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>无报警</Empty>
          )}
        </Panel>

        <Panel title="功能岛负载" flush>
          <table>
            <thead>
              <tr>
                <th>功能岛</th>
                <th className="num">工位</th>
                <th className="num">运行</th>
                <th className="num">故障</th>
                <th className="num">保持中</th>
              </tr>
            </thead>
            <tbody>
              {data.islands.map((island) => (
                <tr key={island.id}>
                  <td>{island.name}</td>
                  <td className="num">{island.stations}</td>
                  <td className="num">{island.running}</td>
                  <td className={`num ${island.fault ? 'bad-text' : ''}`}>{island.fault}</td>
                  <td className="num">{island.held}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <div className="grid cols-2">
        <Panel title="实验方案进展" flush>
          <table>
            <thead>
              <tr>
                <th>方案</th>
                <th>结构</th>
                <th>审批</th>
                <th className="num">样本</th>
                <th>已生成批次</th>
              </tr>
            </thead>
            <tbody>
              {data.plans.map((plan) => (
                <tr key={plan.id}>
                  <td>
                    <Link to={`/plans/${plan.id}`}>{plan.name}</Link>
                    <div className="tiny muted mono">{plan.id} · {plan.plan_type}</div>
                  </td>
                  <td>
                    <Pill state={plan.state} label={plan.state === 'locked' ? '已锁定' : '草稿'} />
                  </td>
                  <td>
                    <Pill state={plan.approval_state} />
                  </td>
                  <td className="num">{plan.sample_count}</td>
                  <td className="small mono">{plan.batches.join('、') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="panel-body small muted">
            「已锁定」是结构冻结，「审批」才决定能不能建正式批次。
          </div>
        </Panel>

        <Panel title="资源与物料提醒" flush>
          {data.materials.expiring.length ||
          data.materials.waste.length ||
          data.resources.expiring_qualifications.length ||
          data.resources.unavailable_assets.length ? (
            <table>
              <thead>
                <tr>
                  <th>对象</th>
                  <th>提示</th>
                </tr>
              </thead>
              <tbody>
                {data.resources.expiring_qualifications.map((row) => (
                  <tr key={row.id}>
                    <td className="small">
                      <Link to="/people">{row.person_name}</Link>
                    </td>
                    <td className="small">
                      {row.label} {row.status === 'expired' ? '已过期' : '即将到期'}
                      {row.expires_at ? `（${row.expires_at.slice(0, 10)}）` : ''}
                    </td>
                  </tr>
                ))}
                {data.resources.unavailable_assets.map((row) => (
                  <tr key={row.id}>
                    <td className="small">
                      <Link to="/assets">{row.asset_no}</Link>
                    </td>
                    <td className="small">{row.unavailable_reasons.join('；')}</td>
                  </tr>
                ))}
                {data.materials.expiring.map((row) => (
                  <tr key={row.lot_id}>
                    <td className="mono">
                      <Link to="/materials">{row.lot_id}</Link>
                    </td>
                    <td className="small">
                      {row.material} {row.expired ? '已过期' : '临近有效期'} {row.effective_expiry}
                      <span className="tiny muted">（依据{row.basis}·{row.release}）</span>
                    </td>
                  </tr>
                ))}
                {data.materials.waste.map((tank) => (
                  <tr key={tank.id}>
                    <td className="mono">{tank.id}</td>
                    <td className="small">
                      {tank.kind} 液位 {tank.level_pct}%，超过 75% 预警线
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>资源与物料无提醒</Empty>
          )}
        </Panel>
      </div>

      <Panel title="最近结果" flush>
        {data.recent_results.length ? (
          <table>
            <thead>
              <tr>
                <th>批次</th>
                <th>流程</th>
                <th className="num">结果 / 待复核 / 可用</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.recent_results.map((row) => (
                <tr key={row.batch_id}>
                  <td className="mono">{row.batch_id}</td>
                  <td>
                    {row.recipe_name}
                    {row.is_golden ? <span className="tag">黄金批次</span> : null}
                  </td>
                  <td className="num mono">
                    {row.typed_results ? (
                      <>
                        {row.result_count} /{' '}
                        <span className={row.pending_review ? 'warn-text' : ''}>{row.pending_review}</span> /{' '}
                        <b>{row.official_count}</b>
                      </>
                    ) : (
                      <span className="tag">历史三指标</span>
                    )}
                  </td>
                  <td>
                    {row.pending_review ? (
                      <Link className="btn sm" to="/data-review">
                        去复核
                      </Link>
                    ) : (
                      <Link className="btn sm" to={`/results/${row.batch_id}`}>
                        结果分析
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>还没有回传的检测结果</Empty>
        )}
      </Panel>
    </div>
  );
}


const percent = (value: number | null | undefined) => (value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`);

/* 运行驾驶舱指标。利用率按设备实际执行指令的时长算，计划负荷按排程时间窗算；两者差得多说明计划不准或设备在等。 */
function KpiSection() {
  const kpi = useQuery<KpiReport>('dashboard:kpi', () => api.get<KpiReport>('/dashboard/kpi?window_hours=24'), 30000);
  const data = kpi.data;
  if (!data) return null;
  const busiest = [...data.utilization.stations].sort((a, b) => b.utilization - a.utilization || b.planned_load - a.planned_load).slice(0, 6);
  return (
    <Panel title="运行指标（最近 24 小时）" aside={<Link to="/exceptions" className="small">异常处理</Link>}>
      <div className="metrics">
        <Metric
          label="实验运行"
          value={data.experiments.running}
          hint={`排队 ${data.experiments.queued} · 暂停 ${data.experiments.paused} · 异常 ${data.experiments.exception}`}
        />
        <Metric label="完成" value={data.experiments.completed} hint={`新建任务 ${data.experiments.tasks_created}`} />
        <Metric
          label="自动化成功率"
          value={percent(data.automation.success_rate)}
          hint={`${data.automation.without_intervention}/${data.automation.completed} 个完成批次全程无人工介入`}
        />
        <Metric
          label="异常"
          value={data.exceptions.raised}
          hint={`自动处理 ${data.exceptions.auto_resolved} · 待处理 ${data.exceptions.open} · 平均恢复 ${
            data.exceptions.mttr_min === null ? '—' : `${data.exceptions.mttr_min} min`
          }`}
        />
        <Metric label="设备利用率" value={percent(data.utilization.overall)} hint="实际执行时长 ÷（时长 × 通道）" />
      </div>
      {busiest.length ? (
        <table>
          <thead>
            <tr>
              <th>工位</th>
              <th>实际利用率</th>
              <th>计划负荷</th>
            </tr>
          </thead>
          <tbody>
            {busiest.map((row) => (
              <tr key={row.station_id}>
                <td className="small">
                  <b className="mono">{row.station_id}</b> <span className="muted">{row.name}</span>
                  {row.channels > 1 ? <span className="tiny muted"> · {row.channels} 通道</span> : null}
                </td>
                <td className="small">
                  <div className="bar" title={`${row.busy_min} min`}>
                    <span style={{ width: `${Math.round(row.utilization * 100)}%` }} />
                  </div>
                  <span className="tiny muted">{percent(row.utilization)}</span>
                </td>
                <td className="small">
                  <div className="bar">
                    <span style={{ width: `${Math.round(row.planned_load * 100)}%` }} />
                  </div>
                  <span className="tiny muted">{percent(row.planned_load)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </Panel>
  );
}
