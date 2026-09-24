import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, pageQuery } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { Paged, PersonRow, PlanRow, StepRunRow, TaskDetail, TaskRow } from '../../shared/types';
import {
  Blocked, ConfirmDialog, Empty, Field, ListState, Modal, NumberInput, Pager, Panel, Pill, useToast,
} from '../../shared/ui';

const STATES: [string, string][] = [
  ['unassigned', '待分配'],
  ['pending_accept', '待接单'],
  ['accepted', '已接单'],
  ['running', '执行中'],
  ['data_review', '待数据复核'],
  ['reporting', '待报告'],
  ['done', '完成'],
  ['cancelled', '已取消'],
];

export function TasksPage() {
  const { user, can } = useSession();
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [state, setState] = useState('');
  const [mine, setMine] = useState(false);
  const [creating, setCreating] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);

  const query = pageQuery({
    page, page_size: 20, state, assignee: mine ? user?.id : undefined,
  });
  const tasks = useQuery<Paged<TaskRow>>(
    `tasks:${query}`, () => api.get<Paged<TaskRow>>(`/experiment-tasks${query}`), 20000,
  );
  const manual = useQuery<StepRunRow[]>(
    'tasks:manual', () => api.get<StepRunRow[]>('/step-runs/mine'), 20000,
  );
  const reviews = useQuery<StepRunRow[]>(
    'tasks:reviews', () => api.get<StepRunRow[]>('/step-runs/reviews'), 20000,
  );

  const rows = tasks.data?.items ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>任务中心</h1>
        <span className="small muted">
          任务状态是派生的：执行阶段来自批次与步骤实例，数据与报告阶段来自检测结果与报告版本。
          手工只能改「谁做、什么时候做」。
        </span>
      </div>

      <div className="split">
        <Panel
          title={`实验任务（${tasks.data?.total ?? 0}）`}
          aside={
            <div className="filters">
              <label className="small">
                <input type="checkbox" checked={mine} onChange={(event) => { setMine(event.target.checked); setPage(1); }} />
                只看我的
              </label>
              <select value={state} onChange={(event) => { setState(event.target.value); setPage(1); }}>
                <option value="">全部状态</option>
                {STATES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
              {can('task.create') ? (
                <button className="btn primary sm" onClick={() => setCreating(true)}>
                  建立任务
                </button>
              ) : null}
            </div>
          }
          flush
        >
          <ListState
            loading={tasks.loading && !tasks.data}
            error={tasks.error}
            empty={!rows.length}
            emptyText="没有符合条件的任务"
          />
          {rows.length ? (
            <table>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>方案</th>
                  <th>执行人</th>
                  <th>截止</th>
                  <th>状态</th>
                  <th>运行</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="clickable" onClick={() => setDetailId(row.id)}>
                    <td>
                      <b className="mono">{row.parent_id ? '↳ ' : ''}{row.id}</b>
                      <div className="tiny muted">{row.title}</div>
                      {row.children.length ? <div className="tiny muted">{row.children.length} 个子任务</div> : null}
                      {row.depends_on.length ? (
                        <div className={`tiny ${row.blocked_by.length ? 'warn-text' : 'muted'}`}>
                          依赖 {row.depends_on.join('、')}{row.blocked_by.length ? '（未满足）' : ''}
                        </div>
                      ) : null}
                    </td>
                    <td className="small">
                      {row.plan_id}
                      <div className="tiny muted">
                        v{row.plan_version} · {row.plan_type}
                      </div>
                    </td>
                    <td className="small">
                      {row.assignee_name || <span className="muted">未分配</span>}
                      {row.reviewer_name ? <div className="tiny muted">复核 {row.reviewer_name}</div> : null}
                    </td>
                    <td className="small">
                      {row.due_at ? clock(row.due_at) : '—'}
                      {row.overdue ? <div className="tiny bad-text">已过期</div> : null}
                    </td>
                    <td>
                      <Pill state={row.state} label={row.state_label} />
                    </td>
                    <td className="small mono">
                      {row.children.length ? (
                        <span className="muted">由子任务执行</span>
                      ) : row.batch_id ? (
                        <button
                          className="btn sm"
                          onClick={(event) => {
                            event.stopPropagation();
                            navigate(`/batches/${row.batch_id}`);
                          }}
                        >
                          {row.batch_id}
                        </button>
                      ) : (
                        <span className="muted">未建批次</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          <Pager
            page={tasks.data?.page ?? 1}
            pageSize={tasks.data?.page_size ?? 20}
            total={tasks.data?.total ?? 0}
            onChange={setPage}
          />
        </Panel>

        <div className="stack">
          <Panel title={`我的人工待办（${manual.data?.length ?? 0}）`} flush>
            {manual.data?.length ? (
              <table>
                <tbody>
                  {manual.data.map((row) => (
                    <tr
                      key={row.id}
                      className="clickable"
                      onClick={() => navigate(`/batches/${row.batch_id}`)}
                    >
                      <td>
                        <span className={`kind ${row.kind}`}>{row.kind_label}</span>{' '}
                        <b>{row.step_name}</b>
                        <div className="tiny muted mono">{row.batch_id}</div>
                      </td>
                      <td className="small">
                        {row.due_at ? clock(row.due_at) : '—'}
                        {row.attempt > 1 ? <div className="tiny warn-text">第 {row.attempt} 次尝试</div> : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>没有分配给你的人工步骤</Empty>
            )}
          </Panel>

          <Panel title={`待流程审核（${reviews.data?.length ?? 0}）`} flush>
            {reviews.data?.length ? (
              <table>
                <tbody>
                  {reviews.data.map((row) => (
                    <tr
                      key={row.id}
                      className="clickable"
                      onClick={() => navigate(`/batches/${row.batch_id}`)}
                    >
                      <td>
                        <span className="kind review">审核</span> <b>{row.step_name}</b>
                        <div className="tiny muted mono">{row.batch_id}</div>
                      </td>
                      <td className="small muted">要求角色 {row.review_role || 'qa'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>没有待审核的流程节点</Empty>
            )}
          </Panel>
        </div>
      </div>

      {creating ? <CreateDialog onClose={() => setCreating(false)} /> : null}
      {detailId ? <TaskDialog taskId={detailId} onOpen={setDetailId} onClose={() => setDetailId(null)} /> : null}
    </div>
  );
}

function TaskDialog({ taskId, onClose, onOpen }: { taskId: string; onClose: () => void; onOpen: (id: string) => void }) {
  const { user, can } = useSession();
  const toast = useToast();
  const navigate = useNavigate();
  const detail = useQuery<TaskDetail>(`tasks:${taskId}`, () => api.get<TaskDetail>(`/experiment-tasks/${taskId}`));
  const [assigning, setAssigning] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const accept = useMutation(() => api.post(`/experiment-tasks/${taskId}/accept`), {
    invalidates: ['tasks', 'dashboard'],
    onSuccess: () => toast.push('已接单'),
  });
  const cancel = useMutation((reason: string) => api.post(`/experiment-tasks/${taskId}/cancel`, { reason }), {
    invalidates: ['tasks', 'dashboard'],
    onSuccess: () => {
      toast.push('任务已取消');
      setCancelling(false);
      onClose();
    },
  });

  const task = detail.data;

  return (
    <Modal title={`任务 · ${taskId}`} onClose={onClose} wide>
      {task ? (
        <>
          <div className="metrics">
            <div className="metric">
              <span className="metric-label">状态</span>
              <strong className="metric-value">
                <Pill state={task.state} label={task.state_label} />
              </strong>
              <span className="metric-hint">由批次、步骤与结果派生</span>
            </div>
            <div className="metric">
              <span className="metric-label">方案版本</span>
              <strong className="metric-value">v{task.plan_version}</strong>
              <span className="metric-hint">{task.plan_id}</span>
            </div>
            <div className="metric">
              <span className="metric-label">执行人</span>
              <strong className="metric-value">{task.assignee_name || '未分配'}</strong>
              <span className="metric-hint">负责人 {task.owner_name || '—'}</span>
            </div>
          </div>

          <div className="panel-aside" style={{ justifyContent: 'flex-end', margin: '10px 0' }}>
            {can('task.assign') ? (
              <button className="btn sm" onClick={() => setAssigning(true)}>
                {task.assignee_user_id ? '转派' : '分配'}
              </button>
            ) : null}
            {task.assignee_user_id === user?.id && !task.accepted_at ? (
              <button
                className="btn primary sm"
                disabled={accept.pending}
                onClick={() => accept.run().catch((error) => toast.push(error.message))}
              >
                接单
              </button>
            ) : null}
            {task.batch_id ? (
              <button className="btn sm" onClick={() => navigate(`/batches/${task.batch_id}`)}>
                打开批次
              </button>
            ) : null}
            {can('task.cancel') && task.state !== 'cancelled' ? (
              <button className="btn sm danger" onClick={() => setCancelling(true)}>
                取消
              </button>
            ) : null}
          </div>

          {task.cancel_reason ? <div className="note warn">取消原因：{task.cancel_reason}</div> : null}

          <TaskStructure task={task} onOpen={onOpen} />

          <Panel title="分配与转派留痕" flush>
            {task.history.length ? (
              <ul className="timeline">
                {task.history.map((row, index) => (
                  <li key={index}>
                    <time>{clock(row.created_at)}</time>
                    <div>
                      <b>{row.action_label}</b>
                      <div className="small">
                        {row.from_name || '—'} → {row.to_name || '—'}（操作人 {row.actor_name}）
                      </div>
                      {row.reason ? <div className="tiny muted">{row.reason}</div> : null}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>还没有分配记录</Empty>
            )}
          </Panel>

          <Panel title="执行与数据" flush>
            <table>
              <tbody>
                <tr>
                  <td className="small muted">步骤实例</td>
                  <td className="small">
                    {task.step_runs.length
                      ? task.step_runs
                          .map((row) => `第 ${row.step_index + 1} 步 ${row.kind}（${row.state}）`)
                          .join('；')
                      : '尚未开跑'}
                  </td>
                </tr>
                <tr>
                  <td className="small muted">检测任务</td>
                  <td className="small">
                    {task.analysis_tasks.length
                      ? task.analysis_tasks.map((row) => `第 ${row.round_no} 轮 ${row.state}`).join('；')
                      : '还没有检测任务'}
                  </td>
                </tr>
                <tr>
                  <td className="small muted">报告</td>
                  <td className="small">
                    {task.report_id ? (
                      <button className="btn sm" onClick={() => navigate('/reports')}>
                        查看报告
                      </button>
                    ) : (
                      '还没有报告'
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
          </Panel>
        </>
      ) : (
        <ListState loading={detail.loading} error={detail.error} />
      )}

      {assigning && task ? (
        <AssignDialog task={task} onClose={() => setAssigning(false)} />
      ) : null}
      {cancelling ? (
        <ConfirmDialog
          title={`取消任务 · ${taskId}`}
          danger
          confirmLabel="取消任务"
          reasonLabel="取消原因"
          pending={cancel.pending}
          error={cancel.error?.message}
          onConfirm={(reason) => cancel.run(reason).catch(() => undefined)}
          onClose={() => setCancelling(false)}
        >
          <div className="note warn">有在途批次时必须先终止批次，再取消任务。</div>
        </ConfirmDialog>
      ) : null}
    </Modal>
  );
}

function AssignDialog({ task, onClose }: { task: TaskDetail; onClose: () => void }) {
  const toast = useToast();
  const people = useQuery<{ items: PersonRow[] }>(
    'people:assignable', () => api.get<{ items: PersonRow[] }>('/people?page_size=100'),
  );
  const [assignee, setAssignee] = useState('');
  const [reason, setReason] = useState('');

  const assign = useMutation(
    () =>
      api.post(`/experiment-tasks/${task.id}/assign`, {
        assignee_user_id: assignee,
        reason,
        row_version: task.row_version,
      }),
    {
      invalidates: ['tasks', 'dashboard'],
      onSuccess: () => {
        toast.push('已分配');
        onClose();
      },
    },
  );

  const candidates = (people.data?.items ?? []).filter((row) => row.user_id);
  const reassign = Boolean(task.assignee_user_id);

  return (
    <Modal
      title={reassign ? '转派任务' : '分配任务'}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!assignee || (reassign && !reason.trim()) || assign.pending}
            onClick={() => assign.run().catch(() => undefined)}
          >
            {reassign ? '转派' : '分配'}
          </button>
        </>
      }
    >
      <div className="note">
        分配时按预计执行时间校验资质。资质到期、被撤销或账号停用的人员会被服务端拒绝，
        并给出具体缺哪一项。
      </div>
      <Field label="执行人">
        <select value={assignee} onChange={(event) => setAssignee(event.target.value)}>
          <option value="">选择执行人</option>
          {candidates.map((row) => (
            <option key={row.user_id} value={row.user_id} disabled={!row.employable}>
              {row.name}
              {row.employable ? '' : '（不在岗或账号停用）'}
            </option>
          ))}
        </select>
      </Field>
      {reassign ? (
        <Field label="转派原因" hint="转派必须留下原因">
          <textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
        </Field>
      ) : null}
      {assign.error ? (
        <div className="note bad">
          {assign.error.message}
          <Blocked reasons={assign.error.blocked.map((row) => row.label)} />
        </div>
      ) : null}
    </Modal>
  );
}

function CreateDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const plans = useQuery<PlanRow[]>('plans', () => api.get<PlanRow[]>('/plans'));
  const [planId, setPlanId] = useState('');
  const [due, setDue] = useState('');
  const [priority, setPriority] = useState(2);

  const create = useMutation(
    () =>
      api.post(
        '/experiment-tasks',
        { plan_id: planId, due_at: due ? new Date(due).toISOString() : null, priority },
        true,
      ),
    {
      invalidates: ['tasks', 'dashboard'],
      onSuccess: () => {
        toast.push('任务已建立');
        onClose();
      },
    },
  );

  const approved = (plans.data ?? []).filter((row) => row.approval_state === 'approved');

  return (
    <Modal
      title="建立实验任务"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={!planId || create.pending} onClick={() => create.run().catch(() => undefined)}>
            建立
          </button>
        </>
      }
    >
      <div className="note">
        只能基于已批准的方案版本建立任务。方案只「锁定」还不够——锁定是结构冻结，不代表已审批。
      </div>
      <Field label="实验方案">
        <select value={planId} onChange={(event) => setPlanId(event.target.value)}>
          <option value="">选择已批准的方案</option>
          {approved.map((row) => (
            <option key={row.id} value={row.id}>
              {row.id} · {row.name}（{row.plan_type_label}）
            </option>
          ))}
        </select>
        {approved.length === 0 ? (
          <span className="small warn-text">当前没有已批准的方案，请先在实验方案页提交评审并由 QA 批准。</span>
        ) : null}
      </Field>
      <Field label="截止时间">
        <input type="datetime-local" value={due} onChange={(event) => setDue(event.target.value)} />
      </Field>
      <Field label="优先级">
        <select value={priority} onChange={(event) => setPriority(Number(event.target.value))}>
          {[1, 2, 3, 4, 5].map((value) => (
            <option key={value} value={value}>
              P{value}
            </option>
          ))}
        </select>
      </Field>
      {create.error ? <div className="note bad">{create.error.message}</div> : null}
    </Modal>
  );
}


/* 任务树与依赖：父任务是容器，状态由子任务汇总；上游任务的批次运行结束后本任务才能下发。 */
function TaskStructure({ task, onOpen }: { task: TaskDetail; onOpen: (id: string) => void }) {
  const { can } = useSession();
  const toast = useToast();
  const [splitting, setSplitting] = useState(false);
  const [chunk, setChunk] = useState<number | ''>('');
  const [parts, setParts] = useState<number | ''>(2);
  const [sequential, setSequential] = useState(false);
  const [editing, setEditing] = useState(false);
  const [deps, setDeps] = useState<string[]>(task.depends_on);
  const candidates = useQuery<Paged<TaskRow>>('tasks:all', () => api.get<Paged<TaskRow>>('/experiment-tasks?page_size=200'));
  const invalidates = ['tasks', 'dashboard', 'batches', 'schedule'];
  const decompose = useMutation(
    () =>
      api.post(
        `/experiment-tasks/${task.id}/decompose`,
        task.sample_ids.length || task.plan_type !== 'matrix'
          ? { chunk_size: chunk === '' ? null : chunk, parts: chunk === '' && parts !== '' ? parts : null, sequential }
          : { parts: parts === '' ? null : parts, sequential },
        true,
      ),
    {
      invalidates,
      onSuccess: () => {
        toast.push('已拆分为子任务');
        setSplitting(false);
      },
    },
  );
  const saveDeps = useMutation(() => api.put(`/experiment-tasks/${task.id}/dependencies`, { depends_on: deps }), {
    invalidates,
    onSuccess: () => {
      toast.push('依赖已更新');
      setEditing(false);
    },
  });
  const others = (candidates.data?.items ?? []).filter((row) => row.id !== task.id && row.state !== 'cancelled');
  const canSplit = can('task.create') && !task.batch_id && !task.children.length && task.state !== 'cancelled';

  return (
    <Panel title="任务树与依赖" flush>
      <div className="panel-body stack">
        {task.parent_id ? (
          <div className="small">
            父任务：<button className="btn sm" onClick={() => onOpen(task.parent_id)}>{task.parent_id}</button>
          </div>
        ) : null}
        {task.children.length ? (
          <table>
            <thead>
              <tr>
                <th>子任务</th>
                <th>批次</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {task.children.map((child) => (
                <tr key={child.id} className="clickable" onClick={() => onOpen(child.id)}>
                  <td className="small">
                    <b className="mono">{child.id}</b> <span className="muted">{child.title}</span>
                  </td>
                  <td className="small mono">{child.batch_id || '—'}</td>
                  <td>
                    <Pill state={child.state} label={child.state_label} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <div className="small">
          <b>上游任务：</b>
          {task.depends_on.length ? task.depends_on.join('、') : '无'}
          {task.blocked_by.length ? (
            <ul className="tiny warn-text">
              {task.blocked_by.map((row) => (
                <li key={row.task_id}>{row.label}</li>
              ))}
            </ul>
          ) : task.depends_on.length ? (
            <span className="tiny ok-text">（已满足）</span>
          ) : null}
        </div>
        <div className="row">
          {can('task.assign') ? (
            <button className="btn sm" onClick={() => { setDeps(task.depends_on); setEditing(!editing); }}>
              {editing ? '收起' : '编辑依赖'}
            </button>
          ) : null}
          {canSplit ? (
            <button className="btn sm" onClick={() => setSplitting(!splitting)}>
              {splitting ? '收起' : '拆分为子任务'}
            </button>
          ) : null}
        </div>
        {editing ? (
          <div className="deps">
            <div className="tiny muted">完成—开始：勾选的任务的批次运行结束后，本任务的批次才能下发；排程也会排在它们之后。</div>
            <div className="dep-list">
              {others.map((row) => (
                <label key={row.id} className="check">
                  <input
                    type="checkbox"
                    checked={deps.includes(row.id)}
                    onChange={(event) =>
                      setDeps(event.target.checked ? [...deps, row.id] : deps.filter((value) => value !== row.id))
                    }
                  />
                  <span className="mono">{row.id}</span> {row.title} <Pill state={row.state} label={row.state_label} />
                </label>
              ))}
            </div>
            <div className="row">
              <button className="btn sm primary" disabled={saveDeps.pending} onClick={() => saveDeps.run().catch(() => undefined)}>
                保存依赖
              </button>
              {saveDeps.error ? <span className="small bad-text">{saveDeps.error.message}</span> : null}
            </div>
          </div>
        ) : null}
        {splitting ? (
          <div className="deps">
            <div className="tiny muted">
              有样本清单时按每份样本数拆（留空按方法的样品位）；矩阵方案或没有样本清单时按份数拆，每份按方案整体执行一次。
              父任务不绑定批次，状态由子任务汇总。
            </div>
            <div className="row">
              {task.sample_ids.length || task.plan_type !== 'matrix' ? (
                <Field label="每份样本数">
                  <NumberInput value={chunk} ariaLabel="每份样本数" onChange={setChunk} />
                </Field>
              ) : null}
              <Field label="份数">
                <NumberInput value={parts} ariaLabel="份数" onChange={setParts} />
              </Field>
              <label className="check">
                <input type="checkbox" checked={sequential} onChange={(event) => setSequential(event.target.checked)} />
                顺序执行（后一份依赖前一份）
              </label>
            </div>
            <div className="row">
              <button className="btn sm primary" disabled={decompose.pending} onClick={() => decompose.run().catch(() => undefined)}>
                拆分
              </button>
              {decompose.error ? <span className="small bad-text">{decompose.error.message}</span> : null}
            </div>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}
