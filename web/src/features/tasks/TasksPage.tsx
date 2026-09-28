import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, pageQuery } from '../../shared/api';
import { clock, num } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import { DEPENDENCY_GATE_LABEL, SPLIT_MODE_HINT } from '../../shared/types';
import type {
  AnalysisView, DependencyGate, Paged, PersonRow, PlanRow, SplitMode, SplitPreview, StepRunRow, TaskDetail, TaskProgress,
  TaskRow,
} from '../../shared/types';
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
  ['shortfall', '待补测'],
  ['cancelled', '已取消'],
];

/** 拆分请求体：填了「每批最多」就按它装满，只填「份数」就均分，都不填按最少批数均分。 */
function splitBody(chunk: number | '', parts: number | '', mode: SplitMode, replicate = false) {
  if (replicate) return { parts: parts === '' ? null : parts, replicate: true, mode };
  return {
    chunk_size: chunk === '' ? null : chunk,
    parts: chunk === '' && parts !== '' ? parts : null,
    replicate: false,
    mode,
  };
}

/** 按样本的进度：计划、有效、失败、在做、待做、补测、放弃、短缺。 */
function ProgressLine({ progress }: { progress: TaskProgress }) {
  return (
    <span className="small">
      计划 <b>{progress.target}</b>：有效 <b className="ok-text">{progress.valid}</b>
      {progress.failed ? <>，失败 <b className="bad-text">{progress.failed}</b></> : null}
      {progress.running ? <>，在做 {progress.running}</> : null}
      {progress.pending ? <>，待做 {progress.pending}</> : null}
      {progress.retest ? <>，补测 {progress.retest}</> : null}
      {progress.accepted ? <>，签名放弃 {progress.accepted}</> : null}
      {progress.descoped ? <>，取消 {progress.descoped}</> : null}
      {progress.shortfall ? <>，<b className="warn-text">短缺 {progress.shortfall}</b></> : null}
    </span>
  );
}

/** 拆分预览与分法：每批最多几个、分几份、整体重复、拆分方式。 */
function SplitControls({
  preview, chunk, parts, mode, replicate, onChunk, onParts, onMode, onReplicate,
}: {
  preview: SplitPreview | undefined;
  chunk: number | '';
  parts: number | '';
  mode: SplitMode;
  replicate?: boolean;
  onChunk: (value: number | '') => void;
  onParts: (value: number | '') => void;
  onMode: (value: SplitMode) => void;
  onReplicate?: (value: boolean) => void;
}) {
  return (
    <div className="stack">
      <div className="row">
        {!replicate ? (
          <Field label="每批最多" hint={`不填按最少批数均分；不能超过流程每批 ${preview?.capacity ?? '—'} 位`}>
            <NumberInput value={chunk} ariaLabel="每批最多" onChange={onChunk} />
          </Field>
        ) : null}
        <Field label="份数" hint={replicate ? '整体执行几次' : '填了「每批最多」时不看它'}>
          <NumberInput value={parts} ariaLabel="份数" onChange={onParts} />
        </Field>
        {onReplicate ? (
          <label className="check" title="每份按方案整体执行一次，而不是把方案的样本分到几批里">
            <input type="checkbox" checked={!!replicate} onChange={(event) => onReplicate(event.target.checked)} />
            整体重复执行
          </label>
        ) : null}
      </div>
      <div className="row">
        {(Object.keys(SPLIT_MODE_HINT) as SplitMode[]).map((key) => (
          <label key={key} className="check" title={SPLIT_MODE_HINT[key]}>
            <input type="radio" name="split-mode" checked={mode === key} onChange={() => onMode(key)} />
            {preview?.modes.find((row) => row.key === key)?.label ?? key}
          </label>
        ))}
      </div>
      <div className="tiny muted">{SPLIT_MODE_HINT[mode]}</div>
      {preview?.error ? <div className="note bad">{preview.error}</div> : null}
      {preview && !preview.error ? <div className="small">{preview.detail}</div> : null}
      {preview?.parts.length ? (
        <table>
          <thead>
            <tr>
              <th>子任务</th>
              <th>份额</th>
              <th className="num">样本数</th>
            </tr>
          </thead>
          <tbody>
            {preview.parts.map((row) => (
              <tr key={row.index}>
                <td className="small">{row.index}/{preview.parts.length}</td>
                <td className="small">
                  {row.label}
                  {row.sample_ids.length ? <div className="tiny muted mono">{row.sample_ids.join('、')}</div> : null}
                </td>
                <td className="num">{row.size}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

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
                      {row.children.length ? (
                        <div className="tiny muted">
                          {row.children.length} 个子任务
                          {row.progress ? (
                            <>
                              {' '}· 有效 {row.progress.valid}/{row.progress.target}
                              {row.progress.shortfall ? <span className="warn-text"> · 短缺 {row.progress.shortfall}</span> : null}
                            </>
                          ) : null}
                        </div>
                      ) : null}
                      {row.purpose === 'retest' ? <div className="tiny warn-text">补测</div> : null}
                      {row.portion_label ? <div className="tiny muted">{row.portion_label}</div> : null}
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
  const [migrating, setMigrating] = useState(false);

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

  const migrate = useMutation(
    (reason: string) => api.post(`/experiment-tasks/${taskId}/migrate-version`, { reason }),
    {
      invalidates: ['tasks', 'dashboard'],
      onSuccess: () => {
        toast.push('已迁移到最新批准版本');
        setMigrating(false);
      },
    },
  );

  const task = detail.data;
  const newer = !!task && task.latest_plan_version != null && task.latest_plan_version > task.plan_version;

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
              <span className="metric-hint">
                {task.plan_id}
                {newer ? <span className="warn-text">（已有批准版本 v{task.latest_plan_version}，任务仍按 v{task.plan_version} 执行）</span> : null}
              </span>
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
            {newer && can('task.assign') && !task.batch_id && task.state !== 'cancelled' ? (
              <button className="btn sm" onClick={() => setMigrating(true)}>
                迁移到 v{task.latest_plan_version}
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
      {migrating && task ? (
        <ConfirmDialog
          title={`迁移方案版本 · ${taskId}`}
          confirmLabel={`迁移到 v${task.latest_plan_version}`}
          reasonLabel="迁移原因"
          pending={migrate.pending}
          error={migrate.error?.message}
          onConfirm={(reason) => migrate.run(reason).catch(() => undefined)}
          onClose={() => setMigrating(false)}
        >
          <div className="note">
            任务建立时锁定了 v{task.plan_version}，方案之后的修订不会静默改变它。迁移后按 v{task.latest_plan_version} 建批次；
            还没建批次的子任务一起迁移，已建批次的子任务保持原版本。
          </div>
        </ConfirmDialog>
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
  const [chunk, setChunk] = useState<number | ''>('');
  const [parts, setParts] = useState<number | ''>('');
  const [mode, setMode] = useState<SplitMode>('parallel');
  const split = splitBody(chunk, parts, mode);
  // 方案超过流程每批样品位时，建任务就按这个分法拆成子任务：先给人看清楚分成几批、每批哪些
  const preview = useQuery<SplitPreview>(
    planId ? `tasks:split-preview:${planId}:${JSON.stringify(split)}` : null,
    () => api.post<SplitPreview>('/experiment-tasks/split-preview', { plan_id: planId, ...split }),
  );
  const needsSplit = !!preview.data?.needs_split;

  const create = useMutation(
    () =>
      api.post(
        '/experiment-tasks',
        {
          plan_id: planId, due_at: due ? new Date(due).toISOString() : null, priority,
          ...(needsSplit ? { split } : {}),
        },
        true,
      ),
    {
      invalidates: ['tasks', 'dashboard'],
      onSuccess: () => {
        toast.push(needsSplit ? `任务已建立，拆成 ${preview.data?.parts.length ?? 0} 个子任务` : '任务已建立');
        onClose();
      },
    },
  );

  const approved = (plans.data ?? []).filter((row) => row.approval_state === 'approved');

  return (
    <Modal
      title="建立实验任务"
      onClose={onClose}
      wide={needsSplit}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!planId || create.pending || (needsSplit && !!preview.data?.error)}
            onClick={() => create.run().catch(() => undefined)}
          >
            {needsSplit ? `建立并拆成 ${preview.data?.parts.length ?? 0} 个子任务` : '建立'}
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
      {planId && preview.data && !needsSplit ? <div className="small muted">{preview.data.detail}</div> : null}
      {needsSplit ? (
        <Panel title="分批执行" flush>
          <div className="panel-body stack">
            <div className="note">
              方案共 {preview.data?.total} 个样本，超过流程每批 {preview.data?.capacity} 个样品位：建立时拆成子任务，
              每个子任务一个批次，父任务汇总进度、出合并报告。
            </div>
            <SplitControls
              preview={preview.data} chunk={chunk} parts={parts} mode={mode}
              onChunk={setChunk} onParts={setParts} onMode={setMode}
            />
          </div>
        </Panel>
      ) : null}
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
      {create.error ? (
        <div className="note bad">
          {create.error.message}
          <Blocked reasons={create.error.blocked.map((row) => row.label)} />
        </div>
      ) : null}
    </Modal>
  );
}


/* 父任务的合并结果：各子任务批次的正式观测合在一起，合并统计、分批明细与批次差异；从这里出合并报告。 */
function TaskResults({ taskId }: { taskId: string }) {
  const { can } = useSession();
  const toast = useToast();
  const navigate = useNavigate();
  const view = useQuery<AnalysisView>(`tasks:${taskId}:results`, () => api.get<AnalysisView>(`/experiment-tasks/${taskId}/results`));
  const report = useMutation(() => api.post('/reports', { task_id: taskId }, true), {
    invalidates: ['reports', 'tasks'],
    onSuccess: () => {
      toast.push('已生成合并报告草稿');
      navigate('/reports');
    },
  });
  const data = view.data;
  return (
    <div className="deps">
      <ListState loading={view.loading && !data} error={view.error} />
      {data ? (
        <>
          <div className="tiny muted">{data.scope_label}；{data.batches?.length ?? 0} 个批次合并</div>
          {data.metrics.length ? (
            data.metrics.map((block) => (
              <div key={block.metric_id} className="stack">
                <div className="small">
                  <b>{block.metric_name}</b>（{block.unit}）：合并 n={block.summary.included}，均值 {num(block.summary.mean, 3)}，
                  SD {num(block.summary.sd, 3)}，CV {num(block.summary.cv_pct, 2)}%
                  {block.summary.excluded ? <span className="muted">；排除 {block.summary.excluded} 条</span> : null}
                </div>
                {block.comparable === false ? (
                  <div className="note warn">{block.comparable_reason}：各批不可比，不能合并出一份报告</div>
                ) : null}
                <table>
                  <thead>
                    <tr>
                      <th>批次</th>
                      <th className="num">纳入</th>
                      <th className="num">排除</th>
                      <th className="num">均值</th>
                      <th className="num">SD</th>
                      <th className="num">CV %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(block.by_batch ?? []).map((row) => (
                      <tr key={row.batch_id}>
                        <td className="small mono">{row.batch_id}</td>
                        <td className="num">{row.n_included}</td>
                        <td className="num">{row.n_excluded}</td>
                        <td className="num">{num(row.mean, 3)}</td>
                        <td className="num">{num(row.sd, 3)}</td>
                        <td className="num">{num(row.cv_pct, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {block.batch_effect ? (
                  <div className={`tiny ${block.batch_effect.significant ? 'warn-text' : 'muted'}`}>
                    批次差异：F({block.batch_effect.df1}, {block.batch_effect.df2}) = {num(block.batch_effect.f, 2)}，
                    p = {block.batch_effect.p === null ? '—' : block.batch_effect.p.toPrecision(3)}；{block.batch_effect.note}
                  </div>
                ) : null}
              </div>
            ))
          ) : (
            <Empty>还没有正式结果：检测结果回传并复核通过后在这里合并</Empty>
          )}
          {can('report.edit') ? (
            <div className="row">
              <button className="btn sm primary" disabled={report.pending} onClick={() => report.run().catch(() => undefined)}>
                出合并报告
              </button>
              <span className="tiny muted">一份报告覆盖全部批次；发布后各子任务算完成</span>
            </div>
          ) : null}
          {report.error ? (
            <div className="note bad">
              {report.error.message}
              <Blocked reasons={report.error.blocked.map((row) => row.label)} />
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}


/* 任务树与依赖：父任务是容器，状态由子任务汇总；上游任务的批次运行结束后本任务才能下发。
   一个方案分多批执行时，父任务按样本汇总进度：短缺要补测或签名放弃，合并结果与报告也在父任务上。 */
function TaskStructure({ task, onOpen }: { task: TaskDetail; onOpen: (id: string) => void }) {
  const { can } = useSession();
  const { sign } = useSignature();
  const toast = useToast();
  const navigate = useNavigate();
  const [splitting, setSplitting] = useState(false);
  const [chunk, setChunk] = useState<number | ''>('');
  const [parts, setParts] = useState<number | ''>('');
  const [replicate, setReplicate] = useState(false);
  const [mode, setMode] = useState<SplitMode>('parallel');
  const [editing, setEditing] = useState(false);
  const [deps, setDeps] = useState<string[]>(task.depends_on);
  const [gate, setGate] = useState<DependencyGate>(task.dependency_gate ?? 'run_completed');
  const [accepting, setAccepting] = useState(false);
  const [showMap, setShowMap] = useState(false);
  const [showResults, setShowResults] = useState(false);
  const candidates = useQuery<Paged<TaskRow>>('tasks:all', () => api.get<Paged<TaskRow>>('/experiment-tasks?page_size=200'));
  const invalidates = ['tasks', 'dashboard', 'batches', 'schedule'];
  const split = splitBody(chunk, parts, mode, replicate);
  const preview = useQuery<SplitPreview>(
    splitting ? `tasks:split-preview:${task.id}:${JSON.stringify(split)}` : null,
    () => api.post<SplitPreview>('/experiment-tasks/split-preview', { task_id: task.id, ...split }),
  );
  const decompose = useMutation(() => api.post(`/experiment-tasks/${task.id}/decompose`, split, true), {
    invalidates,
    onSuccess: () => {
      toast.push('已拆分为子任务');
      setSplitting(false);
    },
  });
  const saveDeps = useMutation(() => api.put(`/experiment-tasks/${task.id}/dependencies`, { depends_on: deps, gate }), {
    invalidates,
    onSuccess: () => {
      toast.push('依赖已更新');
      setEditing(false);
    },
  });
  const makeBatches = useMutation(
    () => api.post<{ batches: { id: string }[] }>(`/experiment-tasks/${task.id}/batches`, {}, true),
    { invalidates, onSuccess: (result) => toast.push(`已为子任务建 ${result.batches.length} 个批次`) },
  );
  const retest = useMutation(() => api.post(`/experiment-tasks/${task.id}/retest`, {}, true), {
    invalidates,
    onSuccess: () => toast.push('已新建补测子任务：照常建批次、排程、执行'),
  });
  const accept = useMutation(
    async (reason: string) => {
      const signatureId = await sign('放弃补测，按现有结果结束', task.id, ['按现有结果结束，不再补测'], task.row_version);
      if (!signatureId) return null;
      return api.post(`/experiment-tasks/${task.id}/accept-shortfall`, { reason, signature_id: signatureId });
    },
    {
      invalidates,
      onSuccess: (result) => {
        if (!result) return;
        toast.push('已签名放弃补测');
        setAccepting(false);
      },
    },
  );
  const others = (candidates.data?.items ?? []).filter((row) => row.id !== task.id && row.state !== 'cancelled');
  const canSplit = can('task.create') && !task.batch_id && !task.children.length && task.state !== 'cancelled';
  const progress = task.children.length ? task.progress : null;
  const waiting = task.children.filter((child) => !child.batch_id && child.state !== 'cancelled');
  const planned = task.children.filter((child) => child.batch_state === 'planned').map((child) => child.batch_id);

  return (
    <Panel title="任务树与依赖" flush>
      <div className="panel-body stack">
        {task.parent_id ? (
          <div className="small">
            父任务：<button className="btn sm" onClick={() => onOpen(task.parent_id)}>{task.parent_id}</button>
            {task.portion_label ? <span className="muted">　本份：{task.portion_label}（{task.planned_count} 个样本）</span> : null}
            {task.purpose === 'retest' ? <span className="warn-text">　补测子任务</span> : null}
          </div>
        ) : null}
        {progress ? (
          <div className="stack">
            <div className="row">
              <ProgressLine progress={progress} />
              {task.split_mode_label ? <span className="tiny muted">　{task.split_mode_label}</span> : null}
            </div>
            {progress.shortfall > 0 ? (
              <div className="note warn">
                还短缺 {progress.shortfall} 个样本（批次终止或样本不合格）：补测，或写明原因签名放弃之后，父任务才能结束，
                依赖它的下游任务也才能放行。
                <div className="row" style={{ marginTop: 6 }}>
                  {can('task.create') ? (
                    <button className="btn sm primary" disabled={retest.pending} onClick={() => retest.run().catch(() => undefined)}>
                      补测 {progress.shortfall} 个
                    </button>
                  ) : null}
                  {can('task.cancel') ? (
                    <button className="btn sm" onClick={() => setAccepting(true)}>
                      放弃补测（签名）
                    </button>
                  ) : null}
                </div>
                {retest.error ? <div className="small bad-text">{retest.error.message}</div> : null}
              </div>
            ) : null}
            {task.shortfall_decisions.length ? (
              <ul className="tiny muted">
                {task.shortfall_decisions.map((row, index) => (
                  <li key={index}>
                    {clock(row.at)} {row.user} 签名放弃 {row.count} 个（计划 {row.target}、有效 {row.valid}）：{row.reason}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        {task.children.length ? (
          <table>
            <thead>
              <tr>
                <th>子任务</th>
                <th>份额</th>
                <th>批次</th>
                <th className="num">有效 / 计划</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {task.children.map((child) => (
                <tr key={child.id} className="clickable" onClick={() => onOpen(child.id)}>
                  <td className="small">
                    <b className="mono">{child.id}</b> <span className="muted">{child.title}</span>
                    {child.depends_on.length ? (
                      <div className="tiny muted">等 {child.depends_on.join('、')}（{child.dependency_gate_label}）</div>
                    ) : null}
                  </td>
                  <td className="small">
                    {child.purpose === 'retest' ? <span className="warn-text">补测 </span> : null}
                    {child.portion_label || '—'}
                  </td>
                  <td className="small mono">
                    {child.batch_id ? (
                      <button
                        className="btn sm"
                        onClick={(event) => {
                          event.stopPropagation();
                          navigate(`/batches/${child.batch_id}`);
                        }}
                      >
                        {child.batch_id}
                      </button>
                    ) : '—'}
                  </td>
                  <td className="num small">
                    {child.valid}/{child.planned_count}
                    {child.failed ? <div className="tiny bad-text">失败 {child.failed}</div> : null}
                  </td>
                  <td>
                    <Pill state={child.state} label={child.state_label} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {task.children.length ? (
          <div className="row">
            {waiting.length && can('batch.create') ? (
              <button className="btn sm primary" disabled={makeBatches.pending} onClick={() => makeBatches.run().catch(() => undefined)}>
                为 {waiting.length} 个子任务建批次
              </button>
            ) : null}
            {planned.length > 1 && can('batch.schedule') ? (
              <button className="btn sm" title="把这几个批次交给多批次优化一起排" onClick={() => navigate(`/schedule?select=${planned.join(',')}`)}>
                一起排程（{planned.length} 批）
              </button>
            ) : null}
            <button className="btn sm" onClick={() => setShowMap(!showMap)}>
              {showMap ? '收起样本对照' : '样本 → 批次对照'}
            </button>
            <button className="btn sm" onClick={() => setShowResults(!showResults)}>
              {showResults ? '收起合并结果' : '合并结果与报告'}
            </button>
          </div>
        ) : null}
        {makeBatches.error ? (
          <div className="note bad">
            {makeBatches.error.message}
            <Blocked reasons={makeBatches.error.blocked.map((row) => row.label)} />
          </div>
        ) : null}
        {showMap && task.sample_map ? (
          <table>
            <thead>
              <tr>
                <th>子任务</th>
                <th>批次</th>
                <th>样本（序号 · 孔位）</th>
              </tr>
            </thead>
            <tbody>
              {task.sample_map.map((row) => (
                <tr key={row.task_id}>
                  <td className="small">
                    <b className="mono">{row.task_id}</b>
                    <div className="tiny muted">{row.purpose === 'retest' ? '补测 ' : ''}{row.label}</div>
                  </td>
                  <td className="small mono">{row.batch_id || <span className="muted">未建批次</span>}</td>
                  <td className="tiny">
                    {row.samples.length ? (
                      row.samples.map((sample, index) => (
                        <span
                          key={`${sample.id}-${index}`}
                          className={`tag${sample.state === 'failed' ? ' warn' : ''}`}
                          title={sample.physical_sample_id}
                        >
                          {sample.repeat ? `#${sample.repeat} ` : ''}
                          {sample.condition_group && sample.condition_group !== 'C01' ? `${sample.condition_group} ` : ''}
                          {sample.well || sample.physical_sample_id}
                          {sample.state === 'failed' ? ' 失败' : ''}
                        </span>
                      ))
                    ) : (
                      <span className="muted">建批次时按份额生成 {row.planned_count} 个样本</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {showResults ? <TaskResults taskId={task.id} /> : null}
        <div className="small">
          <b>上游任务：</b>
          {task.depends_on.length ? `${task.depends_on.join('、')}（${task.dependency_gate_label}后放行）` : '无'}
          {task.inherited_depends_on?.length ? (
            <span className="tiny muted">；从父任务继承 {task.inherited_depends_on.join('、')}</span>
          ) : null}
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
            <button className="btn sm" onClick={() => { setDeps(task.depends_on); setGate(task.dependency_gate ?? 'run_completed'); setEditing(!editing); }}>
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
            <div className="tiny muted">完成—开始：勾选的任务满足放行条件后，本任务的批次才能下发；排程也会排在它们之后。拆出来的子任务继承这些依赖。</div>
            <Field label="放行条件" hint="运行结束：样品做完即可；数据复核通过：要用复核过的数据做决定；报告发布放行：等正式结论">
              <select value={gate} onChange={(event) => setGate(event.target.value as DependencyGate)}>
                {(Object.keys(DEPENDENCY_GATE_LABEL) as DependencyGate[]).map((key) => (
                  <option key={key} value={key}>{DEPENDENCY_GATE_LABEL[key]}</option>
                ))}
              </select>
            </Field>
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
              按样本分份：缺省用最少的批数、各批样本数接近；矩阵方案按重复分，每批都包含全部条件。每份不能超过流程每批样品位。
              勾「整体重复执行」时每份按方案整体做一次。父任务不绑定批次，状态由子任务按样本汇总。
            </div>
            <SplitControls
              preview={preview.data} chunk={chunk} parts={parts} mode={mode} replicate={replicate}
              onChunk={setChunk} onParts={setParts} onMode={setMode} onReplicate={setReplicate}
            />
            <div className="row">
              <button
                className="btn sm primary"
                disabled={decompose.pending || !!preview.data?.error || (preview.data?.parts.length ?? 0) < 2}
                onClick={() => decompose.run().catch(() => undefined)}
              >
                拆分为 {preview.data?.parts.length ?? 0} 个子任务
              </button>
              {decompose.error ? <span className="small bad-text">{decompose.error.message}</span> : null}
            </div>
          </div>
        ) : null}
      </div>
      {accepting && progress ? (
        <ConfirmDialog
          title={`放弃补测 · ${task.id}`}
          danger
          confirmLabel="签名并放弃补测"
          reasonLabel="放弃原因"
          reasonPlaceholder="例如：极片用完，按现有有效样本出结论"
          pending={accept.pending}
          error={accept.error?.message}
          onConfirm={(reason) => accept.run(reason).catch(() => undefined)}
          onClose={() => setAccepting(false)}
        >
          <div className="note warn">
            计划 {progress.target} 个样本，有效完成 {progress.valid} 个，还短缺 {progress.shortfall} 个。放弃后按现有结果结束，
            这条决定连同原因与签名记在父任务上，并写进合并报告的「分批情况」。之后再出现新的短缺还要再处置。
          </div>
        </ConfirmDialog>
      ) : null}
    </Panel>
  );
}
