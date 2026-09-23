import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { clock, dateOf, minutes } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { Gate, OptimizePreview, QueueRow, ScheduleBoard } from '../../shared/types';
import { Empty, GateBanner, Modal, Panel, Pill, useToast } from '../../shared/ui';

const LANE_MINUTES = 8 * 60;

/** 泳道上现在排着哪些批次。排错了要能撤回，否则只能靠终止——那会把样品也报废掉。 */
function ScheduledBatches({
  board,
  gateOpen,
  can,
  onUnschedule,
}: {
  board: ScheduleBoard | undefined;
  gateOpen: boolean;
  can: boolean;
  onUnschedule: { run: (batchId: string) => Promise<unknown>; pending: boolean };
}) {
  const toast = useToast();
  const rows = useMemo(() => {
    const byBatch = new Map<string, { batch_id: string; state: string; stations: Set<string>; from: string; to: string }>();
    for (const lane of board?.stations ?? []) {
      for (const item of lane.items) {
        const row = byBatch.get(item.batch_id) ?? {
          batch_id: item.batch_id, state: item.batch_state, stations: new Set<string>(),
          from: item.starts_at, to: item.ends_at,
        };
        row.stations.add(lane.id);
        if (item.starts_at < row.from) row.from = item.starts_at;
        if (item.ends_at > row.to) row.to = item.ends_at;
        byBatch.set(item.batch_id, row);
      }
    }
    return [...byBatch.values()].sort((a, b) => a.from.localeCompare(b.from));
  }, [board]);

  if (!rows.length) return null;

  return (
    <Panel title={`泳道上的批次（${rows.length}）`} flush>
      <table>
        <thead>
          <tr>
            <th>批次</th>
            <th>状态</th>
            <th>占用工位</th>
            <th>时间窗</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.batch_id}>
              <td>
                <Link to={`/batches/${row.batch_id}`} className="mono">
                  {row.batch_id}
                </Link>
              </td>
              <td>
                <Pill state={row.state} />
              </td>
              <td className="small mono">{[...row.stations].join('、')}</td>
              <td className="small mono">
                {clock(row.from)} → {clock(row.to)}
              </td>
              <td className="row-end">
                {can && row.state === 'scheduled' ? (
                  <button
                    className="btn sm"
                    disabled={onUnschedule.pending || !gateOpen}
                    title={gateOpen ? '退回待排程并归还工位时间窗；物料预留不变' : '执行门关闭'}
                    onClick={() => onUnschedule.run(row.batch_id).catch((error) => toast.push((error as Error).message))}
                  >
                    取消排程
                  </button>
                ) : (
                  <span className="tiny muted">{row.state === 'scheduled' ? '无排程权限' : '已下发，只能终止'}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

export function SchedulePage() {
  const { can } = useSession();
  const toast = useToast();
  const board = useQuery<ScheduleBoard>('schedule:board', () => api.get<ScheduleBoard>('/schedule/board'), 10000);
  const queue = useQuery<QueueRow[]>('schedule:queue', () => api.get<QueueRow[]>('/schedule/queue'), 10000);
  const gate = useQuery<Gate>('gate', () => api.get<Gate>('/gate'), 15000);
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<OptimizePreview | null>(null);

  const schedule = useMutation((batchId: string) => api.post(`/batches/${batchId}/schedule`, {}, true), {
    invalidates: ['schedule', 'batches', 'dashboard'],
    onSuccess: () => toast.push('已写入步骤级分配'),
  });

  const optimize = useMutation((ids: string[]) => api.post<OptimizePreview>('/schedule/optimize', { batch_ids: ids }), {
    invalidates: [],
    onSuccess: setPreview,
  });

  const unschedule = useMutation((batchId: string) => api.post(`/batches/${batchId}/unschedule`), {
    invalidates: ['schedule', 'batches', 'dashboard', 'audit'],
    onSuccess: () => toast.push('已取消排程，工位时间窗已归还'),
  });

  const applyOptimized = useMutation((order: string[]) => api.post('/schedule/optimize/apply', { order }), {
    invalidates: ['schedule', 'batches', 'dashboard'],
    onSuccess: () => {
      toast.push('优化方案已写入');
      setPreview(null);
      setSelected([]);
    },
  });

  const origin = useMemo(() => {
    const starts = (board.data?.stations ?? []).flatMap((lane) => lane.items.map((item) => item.starts_at));
    return starts.length ? new Date(Math.min(...starts.map((value) => dateOf(value).getTime()))) : new Date();
  }, [board.data]);

  const laneOffset = (iso: string) => (minutes(origin.toISOString(), iso) / LANE_MINUTES) * 100;
  const laneWidth = (from: string, to: string) => Math.max(0.6, (minutes(from, to) / LANE_MINUTES) * 100);

  return (
    <div className="page">
      <div className="page-head">
        <h1>排程</h1>
        <div className="row">
          {can('batch.schedule') ? (
            <button
              className="btn"
              disabled={selected.length < 2 || optimize.pending}
              title={selected.length < 2 ? '勾选 2 个以上待排程批次' : undefined}
              onClick={() => optimize.run(selected).catch((error) => toast.push(error.message))}
            >
              优化排程（{selected.length}）
            </button>
          ) : null}
          <span className="small muted">时间轴起点 {clock(origin.toISOString())}，跨度 8 h</span>
        </div>
      </div>
      <GateBanner gate={gate.data} />

      {board.data?.conflicts.length ? (
        <div className="banner bad">
          检测到 {board.data.conflicts.length} 处同工位时间窗重叠：
          {board.data.conflicts.map((conflict) => `${conflict.station_id}（${conflict.a.batch_id}/${conflict.b.batch_id} 重叠 ${conflict.overlap_min} min）`).join('；')}
        </div>
      ) : null}

      <ScheduledBatches board={board.data} gateOpen={!!gate.data?.open} can={can('batch.schedule')} onUnschedule={unschedule} />

      <Panel title="步骤级资源泳道" flush>
        <div className="lanes">
          {(board.data?.stations ?? []).map((lane) => (
            <div key={lane.id} className="lane">
              <div className="lane-head">
                <b className="mono">{lane.id}</b>
                <span className="tiny muted">{lane.name}</span>
                {lane.held ? <span className="tag warn">保持中，释放时间未知</span> : null}
              </div>
              <div className="lane-track">
                {lane.items.map((item, index) => {
                  const width = laneWidth(item.starts_at, item.ends_at);
                  return (
                    <div
                      key={`${item.batch_id}-${item.step_index}-${index}`}
                      className={`chip ${item.kind}${item.uncertain ? ' uncertain' : ''}`}
                      style={{ left: `${laneOffset(item.starts_at)}%`, width: `${width}%` }}
                      title={`${item.batch_id}｜第 ${item.step_index + 1} 步 ${item.step_name ?? ''}｜${clock(item.starts_at)} → ${clock(item.ends_at)}${item.uncertain ? '｜其后安排仅为预测' : ''}`}
                    >
                      {/* 窄条写不下文字，留给悬浮提示，避免半个字符的噪声 */}
                      {width >= 4 ? (
                        <span>
                          {item.kind === 'clean' ? '清洗' : item.kind === 'transfer' ? '转运' : `#${item.batch_id.split('-').pop()}`}
                        </span>
                      ) : null}
                      {item.hard?.maxGapMin && width >= 6 ? <em className="hard">硬</em> : null}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title={`待排程队列（${queue.data?.length ?? 0}）`} flush>
        {queue.data?.length ? (
          <table>
            <thead>
              <tr>
                <th />
                <th>批次</th>
                <th>配方</th>
                <th className="num">优先级</th>
                <th>可排程</th>
                <th>路径预览</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {queue.data.map((row) => (
                <tr key={row.batch_id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.includes(row.batch_id)}
                      onChange={(event) =>
                        setSelected((current) =>
                          event.target.checked
                            ? [...current, row.batch_id]
                            : current.filter((id) => id !== row.batch_id),
                        )
                      }
                    />
                  </td>
                  <td>
                    <Link to={`/batches/${row.batch_id}`} className="mono">
                      {row.batch_id}
                    </Link>
                  </td>
                  <td>
                    {row.recipe}
                    <div className="tiny muted mono">v{row.version} · {row.plan_id}</div>
                  </td>
                  <td className="num">{row.priority}</td>
                  <td>
                    <Pill state={row.schedulable ? 'running' : 'fault'} label={row.schedulable ? '可排程' : '阻塞'} />
                    {row.blocker ? <div className="tiny bad-text">{row.blocker}</div> : null}
                    {!row.material_ok ? <div className="tiny bad-text">物料预留不完整</div> : null}
                  </td>
                  <td className="small muted">
                    {row.schedulable
                      ? `${row.path.filter((step) => step.kind === 'work').map((step) => step.station_id).join(' → ')}（跨度 ${row.makespan_min} min）`
                      : '—'}
                  </td>
                  <td>
                    <button
                      className="btn sm"
                      disabled={!row.schedulable || schedule.pending || !gate.data?.open}
                      onClick={() => schedule.run(row.batch_id).catch((error) => toast.push(error.message))}
                    >
                      单批排程
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>没有待排程批次</Empty>
        )}
      </Panel>

      {preview ? (
        <Modal
          title="优化排程预览"
          wide
          onClose={() => setPreview(null)}
          footer={
            <>
              <button className="btn" onClick={() => setPreview(null)}>
                放弃
              </button>
              <button
                className="btn primary"
                disabled={applyOptimized.pending}
                onClick={() => applyOptimized.run(preview.best.order).catch((error) => toast.push(error.message))}
              >
                确认写入
              </button>
            </>
          }
        >
          <div className="note">
            {preview.method === 'local_search'
              ? `迭代局部搜索评估了 ${preview.evaluated} 个候选顺序（${preview.elapsed_ms ?? 0} ms）`
              : `穷举全部 ${preview.evaluated} 个候选顺序`}
            ，先比总跨度、再比按优先级加权的完成时间。每个候选都由同一个排程器生成时间窗，约束（通道、预约、转运、
            硬时限、依赖）与单批排程完全一致。方案只在确认后写入，下发仍需逐批次开跑检查与电子签名。
          </div>
          {preview.solver ? (
            <div className="small muted">
              {preview.solver.status === 'unavailable'
                ? `CP-SAT：${preview.solver.reason}`
                : `CP-SAT 求解器（${preview.solver.status}${
                    preview.solver.gap_pct !== null && preview.solver.gap_pct !== undefined ? `，距已证明最优 ${preview.solver.gap_pct}%` : ''
                  }，${preview.solver.wall_ms ?? 0} ms）的顺序已作为候选参与比较。${preview.solver.note ?? ''}`}
            </div>
          ) : null}
          <table>
            <thead>
              <tr>
                <th>方案</th>
                <th>顺序</th>
                <th className="num">总跨度</th>
                <th>完成时间</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>基线（按优先级）</td>
                <td className="mono small">{preview.baseline.order.join(' → ')}</td>
                <td className="num">{preview.baseline.ok ? `${preview.baseline.span_min} min` : '不可行'}</td>
                <td className="small mono">{preview.baseline.ok ? clock(preview.baseline.finish_at) : preview.baseline.reason}</td>
              </tr>
              <tr className="current">
                <td>
                  <b>优化方案</b>
                </td>
                <td className="mono small">{preview.best.order.join(' → ')}</td>
                <td className="num">
                  <b>{preview.best.span_min} min</b>
                </td>
                <td className="small mono">{clock(preview.best.finish_at)}</td>
              </tr>
            </tbody>
          </table>
          <div className="small muted">
            {preview.improvement_min !== null && preview.improvement_min > 0
              ? `相比基线缩短 ${preview.improvement_min} min。`
              : '与基线相同，无需改动。'}
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
