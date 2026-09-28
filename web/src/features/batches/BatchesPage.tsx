import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { BatchSummary, Gate, Paged, PlanSummary, TaskRow } from '../../shared/types';
import { ConfirmDialog, Empty, Field, GateBanner, Modal, Panel, Pill, useToast } from '../../shared/ui';

export function BatchesPage() {
  const { can } = useSession();
  const toast = useToast();
  const batches = useQuery<BatchSummary[]>('batches', () => api.get<BatchSummary[]>('/batches'), 10000);
  const plans = useQuery<PlanSummary[]>('plans', () => api.get<PlanSummary[]>('/plans'));
  const gate = useQuery<Gate>('gate', () => api.get<Gate>('/gate'), 15000);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<BatchSummary | null>(null);
  const [planId, setPlanId] = useState('');
  const [taskId, setTaskId] = useState('');
  const [priority, setPriority] = useState(2);
  const [note, setNote] = useState('');
  // 任务中心派下来的任务：批次与它绑定，任务状态随批次推进。不选时服务端补建一条（执行人是建批次的人）
  const tasks = useQuery<Paged<TaskRow>>(
    creating && planId ? `tasks:for-batch:${planId}` : null,
    () => api.get<Paged<TaskRow>>(`/experiment-tasks?plan_id=${encodeURIComponent(planId)}&page_size=100`),
  );
  const openTasks = (tasks.data?.items ?? []).filter(
    (task) => !task.batch_id && !task.children.length && !['cancelled', 'done'].includes(task.state),
  );

  const create = useMutation(
    () =>
      api.post<BatchSummary>(
        '/batches', { plan_id: planId, priority, note, ...(taskId ? { task_id: taskId } : {}) }, true,
      ),
    {
      invalidates: ['batches', 'dashboard', 'plans', 'lots', 'tasks'],
      onSuccess: (batch) => {
        toast.push(`${batch.id} 已创建，物料已按 BOM 预留`);
        setCreating(false);
      },
    },
  );

  const schedule = useMutation((batchId: string) => api.post<BatchSummary>(`/batches/${batchId}/schedule`, {}, true), {
    invalidates: ['batches', 'schedule', 'dashboard'],
    onSuccess: (batch) => toast.push(`${batch.id} 已排程`),
  });

  const lockedPlans = (plans.data ?? []).filter((plan) => plan.state === 'locked');
  const rows = batches.data ?? [];

  const unschedule = useMutation((batchId: string) => api.post<BatchSummary>(`/batches/${batchId}/unschedule`), {
    invalidates: ['batches', 'schedule', 'dashboard', 'audit'],
    onSuccess: () => toast.push('已取消排程，工位时间窗已归还；物料预留保持不变'),
  });

  const remove = useMutation((batchId: string) => api.remove(`/batches/${batchId}`), {
    invalidates: ['batches', 'schedule', 'dashboard', 'reservations', 'lots', 'audit'],
    onSuccess: () => {
      toast.push('批次已删除，工位时间窗与物料预留已归还');
      setDeleting(null);
    },
  });

  return (
    <div className="page">
      <div className="page-head">
        <h1>批次管理</h1>
        {can('batch.create') ? (
          <button
            className="btn primary"
            onClick={() => {
              setPlanId(lockedPlans[0]?.id ?? '');
              setTaskId('');
              setCreating(true);
            }}
          >
            新建批次
          </button>
        ) : null}
      </div>
      <GateBanner gate={gate.data} />

      <Panel title={`全部批次（${rows.length}）`} flush>
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>批次</th>
                <th>流程 / 计划</th>
                <th>状态</th>
                <th>下一步动作</th>
                <th>计划时间</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((batch) => (
                <tr key={batch.id}>
                  <td>
                    <Link to={`/batches/${batch.id}`} className="mono">
                      {batch.id}
                    </Link>
                    <div className="tiny muted">优先级 {batch.priority} · {batch.operator}</div>
                  </td>
                  <td>
                    {batch.recipe_name}
                    <div className="tiny muted mono">
                      {batch.recipe_id} v{batch.version} · {batch.plan_id}
                    </div>
                  </td>
                  <td>
                    <Pill state={batch.state} label={batch.state_label} />
                    {batch.failure_reason ? <div className="tiny bad-text">{batch.failure_reason}</div> : null}
                  </td>
                  <td>
                    <b>{batch.next_action.what}</b>
                    <div className="tiny muted">{batch.next_action.why}</div>
                  </td>
                  <td className="small mono">
                    {clock(batch.starts_at)} → {clock(batch.ends_at)}
                  </td>
                  <td className="row-end">
                    {batch.state === 'planned' && can('batch.schedule') ? (
                      <button
                        className="btn sm"
                        disabled={schedule.pending || !gate.data?.open}
                        onClick={() =>
                          schedule.run(batch.id).catch((error) => toast.push(error.message))
                        }
                      >
                        排程
                      </button>
                    ) : null}
                    {batch.state === 'scheduled' && can('batch.schedule') ? (
                      <button
                        className="btn sm"
                        disabled={unschedule.pending || !gate.data?.open}
                        title={gate.data?.open ? '退回待排程并归还工位时间窗' : '执行门关闭'}
                        onClick={() => unschedule.run(batch.id).catch((error) => toast.push(error.message))}
                      >
                        取消排程
                      </button>
                    ) : null}
                    <Link className="btn sm" to={`/batches/${batch.id}`}>
                      详情
                    </Link>
                    {can('batch.control') ? (
                      <button
                        className="btn sm danger"
                        disabled={batch.delete_blockers.length > 0}
                        title={batch.delete_blockers.join('；') || undefined}
                        onClick={() => setDeleting(batch)}
                      >
                        删除
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>还没有批次。实验方案要先锁定结构、再提交评审并由 QA 批准，才能建立批次。</Empty>
        )}
      </Panel>

      {deleting ? (
        <ConfirmDialog
          title={`删除批次 · ${deleting.id}`}
          danger
          confirmLabel="删除"
          pending={remove.pending}
          error={remove.error?.message}
          onClose={() => setDeleting(null)}
          onConfirm={() => remove.run(deleting.id).catch(() => undefined)}
        >
          <div className="note warn">
            该批次尚未下发设备，删除会一并归还它占的工位时间窗与物料预留，
            {deleting.sample_count} 个未开跑的样品记录同时移除。删除动作写审计。
          </div>
          <div className="small muted">
            已下发的批次不走这条路：设备动过，指令与检查点是设备行为的记录，只能「终止」。
          </div>
        </ConfirmDialog>
      ) : null}

      {creating ? (
        <Modal
          title="新建批次"
          onClose={() => setCreating(false)}
          footer={
            <>
              <button className="btn" onClick={() => setCreating(false)}>
                取消
              </button>
              <button
                className="btn primary"
                disabled={!planId || create.pending}
                onClick={() => create.run().catch((error) => toast.push(error.message))}
              >
                创建并预留物料
              </button>
            </>
          }
        >
          <div className="note">
            批次只能从已锁定矩阵的实验计划创建。创建时冻结流程快照、按 BOM 选择已放行批号写入预留、
            按条件矩阵生成样品孔位，三者在同一事务内完成。
          </div>
          <Field label="实验计划">
            <select
              value={planId}
              onChange={(event) => {
                setPlanId(event.target.value);
                setTaskId('');
              }}
            >
              {lockedPlans.map((plan) => (
                <option key={plan.id} value={plan.id}>
                  {plan.id} · {plan.name}（{plan.sample_count} 样品）
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="实验任务"
            hint="任务中心分配、接单的任务选在这里，批次与它绑定；不选则系统补建一条任务，执行人是你"
          >
            <select value={taskId} disabled={!planId} onChange={(event) => setTaskId(event.target.value)}>
              <option value="">不关联已有任务（系统补建）</option>
              {openTasks.map((task) => (
                <option key={task.id} value={task.id}>
                  {task.id} · 执行人 {task.assignee_name || '未分配'} · {task.state_label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="优先级" hint="1 最高，5 最低。排程队列按优先级排序。">
            <input
              type="number"
              min={1}
              max={5}
              value={priority}
              onChange={(event) => setPriority(Number(event.target.value))}
            />
          </Field>
          <Field label="备注">
            <textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
          </Field>
          {create.error ? <div className="note bad">{create.error.message}</div> : null}
        </Modal>
      ) : null}
    </div>
  );
}
