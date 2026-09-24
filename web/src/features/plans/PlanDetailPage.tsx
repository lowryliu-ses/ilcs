import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api } from '../../shared/api';
import { num } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { CommentsPanel } from '../../shared/comments';
import type { ApprovalLevel, DesignSpace, DiffRow, Factor, LotRow, MetricRow, PlanDetail, ProposalRow } from '../../shared/types';
import { useSignature } from '../../shared/signature';
import {
  Blocked, CheckList, ConfirmDialog, Empty, Field, Modal, NumberInput, Panel, Pill, useToast,
} from '../../shared/ui';

export function PlanDetailPage() {
  const { planId = '' } = useParams();
  const navigate = useNavigate();
  const { can } = useSession();
  const toast = useToast();
  const { sign } = useSignature();
  const plan = useQuery<PlanDetail>(`plans:${planId}`, () => api.get<PlanDetail>(`/plans/${planId}`));
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [comparing, setComparing] = useState<number | null>(null);

  const invalidates = [`plans:${planId}`, 'plans', 'dashboard'];
  const lock = useMutation(() => api.post(`/plans/${planId}/lock`), {
    invalidates,
    // 锁定只是结构冻结：提示里不能说「可以建批次了」，那要等审批通过
    onSuccess: () => toast.push('结构已锁定；还需提交评审并由 QA 批准才能建立任务与批次'),
  });
  const unlock = useMutation(() => api.post(`/plans/${planId}/unlock`), {
    invalidates,
    onSuccess: () => toast.push('结构已解锁'),
  });
  const remove = useMutation(() => api.remove(`/plans/${planId}`), {
    invalidates: ['plans', 'dashboard', 'audit'],
    onSuccess: () => {
      toast.push('方案草稿已删除');
      navigate('/plans');
    },
  });
  const saveTemplate = useMutation(
    (name: string) => api.post('/plans/templates', { name, from_plan_id: planId }),
    { invalidates: ['plans:templates'], onSuccess: () => toast.push('已存为方案模板，新建方案时可以套用') },
  );
  const withdraw = useMutation(() => api.post(`/plans/${planId}/withdraw`, { reason: '撤回修改' }), {
    invalidates,
    onSuccess: () => toast.push('已撤回评审，回到草稿'),
  });
  const restore = useMutation(
    (version: number) => api.post(`/plans/${planId}/restore`, { from_version: version, row_version: plan.data?.row_version }),
    { invalidates, onSuccess: () => toast.push('已把历史版本内容恢复到当前草稿；历史版本不变') },
  );
  const decide = useMutation(
    (payload: { conclusion: string; reason?: string; signature_id?: string }) =>
      api.post(`/plans/${planId}/decision`, payload),
    {
      invalidates,
      onSuccess: (result) => {
        const row = result as PlanDetail;
        toast.push(
          row.approval_state === 'approved'
            ? '已批准；该版本冻结'
            : row.approval_state === 'review'
              ? '本级已通过，等待下一级审批'
              : '已驳回；作者修改后可重新提交',
        );
        setRejecting(false);
      },
    },
  );
  const revise = useMutation(() => api.post(`/plans/${planId}/revisions`), {
    invalidates,
    onSuccess: () => toast.push('已生成新版本草稿；原批准版本快照保留'),
  });

  if (!plan.data) return <div className="boot">{plan.error ? plan.error.message : '加载中…'}</div>;
  const data = plan.data;
  // 结构可编辑 = 未锁定且未批准。锁定是结构冻结，批准是审批结论，两件事都能挡住编辑。
  const editable = data.state === 'draft' && ['draft', 'rejected'].includes(data.approval_state) && can('plan.edit');
  const currentLevel = data.approval_state === 'review' ? data.approvals.find((row) => !row.conclusion) : undefined;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>
            {data.name} <Pill state={data.state} label={data.state_label} />{' '}
            <Pill state={data.approval_state} label={`审批：${data.approval_label}`} />
          </h1>
          <div className="small muted">
            <span className="tag">{data.plan_type_label}</span>{' '}
            <span className="mono">{data.id}</span> v{data.version} · 流程{' '}
            <Link to={`/recipes/${data.recipe_id}`}>{data.recipe_id}</Link> · {data.owner} · {data.created}
          </div>
        </div>
        <div className="row">
          {editable ? (
            <button className="btn" onClick={() => setEditing(true)}>
              {data.is_matrix ? '编辑因子' : '编辑方案'}
            </button>
          ) : null}
          {editable ? (
            <button
              className="btn primary"
              disabled={!data.lockable || lock.pending}
              title={data.lockable ? undefined : '锁定校验未通过'}
              onClick={() => lock.run().catch((error) => toast.push(error.message))}
            >
              {data.is_matrix ? '锁定矩阵' : '锁定结构'}
            </button>
          ) : null}
          {data.state === 'locked' && data.approval_state !== 'approved' && can('plan.edit') ? (
            <button className="btn" onClick={() => unlock.run().catch((error) => toast.push(error.message))}>
              解锁
            </button>
          ) : null}
          {data.approval_state === 'review' && can('plan.edit') ? (
            <button className="btn" disabled={withdraw.pending} onClick={() => withdraw.run().catch((error) => toast.push(error.message))}>
              撤回评审
            </button>
          ) : null}
          {['draft', 'rejected'].includes(data.approval_state) && can('plan.submit') ? (
            <button
              className="btn"
              disabled={!data.lockable}
              title={data.lockable ? undefined : '校验未通过，先补齐再提交'}
              onClick={() => setSubmitting(true)}
            >
              {data.approval_state === 'rejected' ? '重新提交评审' : '提交评审'}
            </button>
          ) : null}
          {can('plan.edit') ? (
            <button
              className="btn"
              disabled={saveTemplate.pending}
              onClick={() => {
                const name = window.prompt('模板名称', `${data.name} 模板`);
                if (name?.trim()) saveTemplate.run(name.trim()).catch((error) => toast.push(error.message));
              }}
            >
              存为模板
            </button>
          ) : null}
          {data.approval_state === 'review' && can('plan.approve') ? (
            <>
              <button className="btn" onClick={() => setRejecting(true)}>
                驳回{currentLevel ? `（${currentLevel.label}）` : ''}
              </button>
              <button
                className="btn primary"
                onClick={() =>
                  sign('批准实验方案', data.id, ['批准实验方案'], data.row_version)
                    .then((signatureId) =>
                      signatureId ? decide.run({ conclusion: 'approved', signature_id: signatureId }) : undefined,
                    )
                    .catch((error) => toast.push(error.message))
                }
              >
                批准
              </button>
            </>
          ) : null}
          {data.approval_state === 'approved' && can('plan.edit') ? (
            <button
              className="btn"
              disabled={revise.pending}
              title="批准版本不可修改；修订生成新版本，历史运行不受影响"
              onClick={() => revise.run().catch((error) => toast.push(error.message))}
            >
              修订
            </button>
          ) : null}
          {can('plan.edit') ? (
            <button
              className="btn danger"
              disabled={data.delete_blockers.length > 0}
              title={data.delete_blockers.join('；') || undefined}
              onClick={() => setDeleting(true)}
            >
              删除
            </button>
          ) : null}
        </div>
      </div>

      <div className="note">
        <b>研究目标：</b>
        {data.goal || '未填写'}
        <div className="small muted">
          「{data.state_label}」是结构冻结，「审批：{data.approval_label}」才是审批结论。
          只有已批准的版本能建立实验任务与正式批次。
        </div>
      </div>
      {data.reject_reason ? <div className="note warn">驳回理由：{data.reject_reason}</div> : null}

      <div className="grid cols-2">
        {data.is_matrix ? (
        <Panel title="因子与水平" flush>
          <table>
            <thead>
              <tr>
                <th>因子</th>
                <th>水平</th>
                <th>作用于设备参数</th>
                <th>物料换算</th>
              </tr>
            </thead>
            <tbody>
              {data.factors.map((factor) => (
                <tr key={factor.name}>
                  <td>{factor.name}</td>
                  <td className="mono">
                    {factor.levels.join('、')}
                    {factor.unit}
                  </td>
                  <td className="small mono">
                    {factor.target
                      ? `${data.target_options?.find((o) => o.step_id === factor.target?.step_id)?.step_name ?? factor.target.step_id}.${factor.target.param}`
                      : <span className="muted">仅区分样本</span>}
                  </td>
                  <td className="small muted">
                    {factor.material ? `${factor.material.name} ${factor.material.per}${factor.material.unit}/单位` : '—'}
                  </td>
                </tr>
              ))}
              {data.factors.length === 0 ? (
                <tr>
                  <td colSpan={4} className="muted">
                    未定义因子
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
          <div className="panel-body small muted">
            重复 {data.repeats} 次 · 布局 {data.layout === 'randomized' ? `随机化（种子 ${data.seed}）` : '顺序'} ·
            对照 {data.control?.label ?? '未设'}
          </div>
        </Panel>
        ) : (
          <Panel title="样本选择">
            <div className="note">
              {data.plan_type_label}不使用因子矩阵，也不要求两个因子水平。
            </div>
            <table>
              <tbody>
                <tr>
                  <td className="small muted">样本数</td>
                  <td className="mono">{data.sample_count}</td>
                </tr>
                <tr>
                  <td className="small muted">样本清单</td>
                  <td className="small mono">
                    {data.sample_ids.length ? data.sample_ids.join('、') : '按样本数生成'}
                  </td>
                </tr>
                <tr>
                  <td className="small muted">流程版本</td>
                  <td className="small">{data.method_version || '—'}</td>
                </tr>
              </tbody>
            </table>
          </Panel>
        )}

        <Panel title="结构校验">
          <CheckList checks={data.checks} />
          <div className="small muted">
            这份校验决定能不能锁定与提交评审；按方案类型分支，不适用的项不会出现。
          </div>
        </Panel>
      </div>

      <div className="grid cols-2">
        <Panel title={`所需检测指标（${data.metrics.length}）`} flush>
          {data.metrics.length ? (
            <table>
              <thead>
                <tr>
                  <th>指标</th>
                  <th>版本</th>
                  <th>单位</th>
                  <th>值类型</th>
                </tr>
              </thead>
              <tbody>
                {data.metrics.map((row) => (
                  <tr key={row.id}>
                    <td>
                      {row.name}
                      <div className="tiny muted mono">{row.code}</div>
                    </td>
                    <td className="mono small">{row.version}</td>
                    <td className="mono small">{row.unit || '—'}</td>
                    <td className="small">
                      {row.value_type === 'number' ? '数值' : row.value_type === 'enum' ? '枚举' : '文本'}
                      {row.value_type === 'number' ? '' : <div className="tiny muted">不进数值统计</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>未指定所需检测指标；方案批准前必须补上</Empty>
          )}
          <div className="panel-body small muted">
            建检测任务时会冻结这份集合；之后改指标定义不影响已建任务的要求。
          </div>
        </Panel>

        <Panel title={`版本与审批（当前 v${data.version}）`} flush>
          {data.versions.length ? (
            <table>
              <thead>
                <tr>
                  <th>版本</th>
                  <th>状态</th>
                  <th>编写</th>
                  <th>批准</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.versions.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">v{row.version}</td>
                    <td>
                      <Pill state={row.state} label={row.state_label} />
                      {row.reject_reason ? <div className="tiny muted">{row.reject_reason}</div> : null}
                    </td>
                    <td className="small">{row.author_name || '—'}</td>
                    <td className="small">
                      {row.approver_name || '—'}
                      {row.approved_at ? <div className="tiny muted">{row.approved_at.slice(0, 16)}</div> : null}
                    </td>
                    <td className="row-end">
                      <button className="btn sm" onClick={() => setComparing(row.version)}>
                        对比当前
                      </button>
                      {editable && row.version <= data.version ? (
                        <button
                          className="btn sm"
                          disabled={restore.pending}
                          onClick={() => restore.run(row.version).catch((error) => toast.push(error.message))}
                        >
                          恢复此版内容
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>还没有提交过评审</Empty>
          )}
          {data.approvals.length ? <ApprovalProgress levels={data.approvals} /> : null}
          <div className="panel-body small muted">
            批准版本不可修改；修订生成新版本，历史运行仍引用原快照。逐级审批：前一级通过后一级才能审，
            作者不能审任何一级，同一个人不能审两级。
          </div>
        </Panel>
      </div>

      {data.is_matrix ? (
      <Panel title={`条件矩阵（${data.conditions.length} 组，${data.sample_count} 样品）`} flush>
        <table>
          <thead>
            <tr>
              <th>条件组</th>
              <th>水平组合</th>
              <th>孔位</th>
            </tr>
          </thead>
          <tbody>
            {data.conditions.map((condition) => (
              <tr key={condition.group}>
                <td className="mono">
                  {condition.group}
                  {condition.is_control ? <span className="tag">对照</span> : null}
                </td>
                <td>{condition.label}</td>
                <td className="mono small">
                  {data.layout_preview
                    .filter((well) => well.group === condition.group)
                    .map((well) => well.well)
                    .join(' ')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      ) : null}

      {data.materials.length ? (
      <Panel title="物料需求预览（每批）" flush>
        <table>
          <thead>
            <tr>
              <th>来源</th>
              <th>物料</th>
              <th className="num">需求</th>
              <th className="num">已放行可用</th>
              <th>批号</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.materials.map((row, index) => (
              <tr key={`${row.material}-${index}`}>
                <td className="small muted">{row.source}</td>
                <td>{row.material}</td>
                <td className="num">
                  {num(row.qty, 3)} {row.unit}
                </td>
                <td className="num mono">
                  {row.available} {row.unit}
                </td>
                <td className="mono small">{row.lots.join('、') || '无已放行批号'}</td>
                <td>
                  <Pill state={row.ok ? 'running' : 'fault'} label={row.ok ? '可预留' : '不足'} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="panel-body small muted">
          创建批次时按流程 BOM 写入预留；因子换算物料需先入库并放行，否则开跑检查会拦截。
        </div>
      </Panel>
      ) : (
        <Panel title="物料需求预览">
          <div className="note">该方案没有物料需求（委托检测不强制定义耗材，空 BOM 的流程显示「无需物料」）。</div>
        </Panel>
      )}

      {deleting ? (
        <ConfirmDialog
          title={`删除实验计划 · ${data.id}`}
          danger
          confirmLabel="删除"
          pending={remove.pending}
          error={remove.error?.message}
          onClose={() => setDeleting(false)}
          onConfirm={() => remove.run().catch(() => undefined)}
        >
          <div className="note warn">
            将删除「{data.name}」及其 {data.conditions.length} 组条件与孔位布局定义。
            流程 {data.recipe_id} 不受影响。
          </div>
        </ConfirmDialog>
      ) : null}

      {editing ? <FactorEditor plan={data} onClose={() => setEditing(false)} invalidates={invalidates} /> : null}

      {data.is_matrix ? <CampaignPanel plan={data} invalidates={invalidates} /> : null}

      <CommentsPanel
        targetType="plan"
        targetId={data.id}
        anchors={[['goal', '目的'], ['factors', '因子与水平'], ['sample_count', '样本'], ['required_metrics', '检测指标'], ['layout', '布局']]}
      />

      {submitting ? <SubmitDialog plan={data} invalidates={invalidates} onClose={() => setSubmitting(false)} /> : null}
      {comparing !== null ? <DiffDialog planId={data.id} from={comparing} onClose={() => setComparing(null)} /> : null}
    </div>
  );
}

function ApprovalProgress({ levels }: { levels: ApprovalLevel[] }) {
  return (
    <div className="panel-body">
      <div className="small">
        <b>逐级审批</b>
      </div>
      <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>
        {levels.map((row) => (
          <li key={row.level}>
            {row.label}
            {row.assignee_name ? <span className="muted">（指定 {row.assignee_name}）</span> : null}：
            {row.conclusion === 'approved' ? (
              <span> 已通过 · {row.decided_by_name} {row.decided_at?.slice(0, 16).replace('T', ' ')}</span>
            ) : row.conclusion === 'rejected' ? (
              <span className="bad-text"> 已驳回 · {row.decided_by_name}：{row.reason}</span>
            ) : (
              <span className="muted"> 待审</span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

type Approver = { id: string; display_name: string; roles: string[] };

/** 提交评审：可以设置多级审批并给每级指定审批人；不设就是一级「QA 审批」。 */
function SubmitDialog({ plan, invalidates, onClose }: { plan: PlanDetail; invalidates: string[]; onClose: () => void }) {
  const toast = useToast();
  const approvers = useQuery<Approver[]>('plans:approvers', () => api.get<Approver[]>('/plans/approvers'));
  const [levels, setLevels] = useState<{ label: string; assignee_id: string }[]>([{ label: 'QA 审批', assignee_id: '' }]);
  const submit = useMutation(() => api.post(`/plans/${plan.id}/submit`, { approvers: levels }), {
    invalidates,
    onSuccess: () => {
      toast.push(`已提交评审（${levels.length} 级）；全部通过后才能建立实验任务与正式批次`);
      onClose();
    },
  });
  const assigned = levels.map((row) => row.assignee_id).filter(Boolean);
  const duplicate = assigned.length !== new Set(assigned).size;
  return (
    <Modal
      title={`提交评审 · ${plan.id} v${plan.version}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={submit.pending || duplicate || !levels.length} onClick={() => submit.run().catch(() => undefined)}>
            提交
          </button>
        </>
      }
    >
      <div className="note">
        按顺序逐级审：前一级通过后一级才能审，任一级驳回即结束本次评审。每级可以指定审批人，不指定则任何有批准权限的人都可以审；
        同一个人不能被指定两级，作者本人不能审批。
      </div>
      {levels.map((row, index) => (
        <div key={index} className="filters">
          <span className="small mono">第 {index + 1} 级</span>
          <input
            value={row.label}
            placeholder="级别名称，如 技术审核"
            onChange={(event) => setLevels(levels.map((item, at) => (at === index ? { ...item, label: event.target.value } : item)))}
          />
          <select
            value={row.assignee_id}
            onChange={(event) => setLevels(levels.map((item, at) => (at === index ? { ...item, assignee_id: event.target.value } : item)))}
          >
            <option value="">不指定</option>
            {(approvers.data ?? []).map((person) => (
              <option key={person.id} value={person.id}>
                {person.display_name}（{person.roles.join('、')}）
              </option>
            ))}
          </select>
          {levels.length > 1 ? (
            <button className="btn sm" onClick={() => setLevels(levels.filter((_, at) => at !== index))}>
              删除
            </button>
          ) : null}
        </div>
      ))}
      {levels.length < 5 ? (
        <button className="btn sm" onClick={() => setLevels([...levels, { label: `第 ${levels.length + 1} 级审批`, assignee_id: '' }])}>
          增加一级
        </button>
      ) : null}
      {duplicate ? <div className="note bad">同一个人不能被指定审批两级</div> : null}
      {submit.error ? <div className="note bad">{submit.error.message}</div> : null}
    </Modal>
  );
}

function DiffDialog({ planId, from, onClose }: { planId: string; from: number; onClose: () => void }) {
  const diff = useQuery<{ from: string; to: string; changes: DiffRow[] }>(`plans:${planId}:diff:${from}`, () =>
    api.get(`/plans/${planId}/diff?from_version=${from}`),
  );
  return (
    <Modal title={`版本对比 · ${diff.data ? `${diff.data.from} → ${diff.data.to}` : ''}`} wide onClose={onClose}>
      {diff.data ? (
        diff.data.changes.length ? (
          <table>
            <thead>
              <tr>
                <th>字段</th>
                <th>{diff.data.from}</th>
                <th>{diff.data.to}</th>
              </tr>
            </thead>
            <tbody>
              {diff.data.changes.map((row) => (
                <tr key={row.field}>
                  <td className="small">{row.label}</td>
                  <td className="small mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{row.before}</td>
                  <td className="small mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{row.after}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>两个版本内容相同</Empty>
        )
      ) : (
        <div className="muted">{diff.error ? diff.error.message : '加载中…'}</div>
      )}
    </Modal>
  );
}

/* 闭环实验活动：设计空间随审批冻结，外部优化器的提案超出它一律拒绝；
   接受的提案只生成下一轮方案草稿，仍需锁定、提交并由 QA 批准。 */
function CampaignPanel({ plan, invalidates }: { plan: PlanDetail; invalidates: string[] }) {
  const { can } = useSession();
  const toast = useToast();
  const [editingSpace, setEditingSpace] = useState(false);
  const proposals = useQuery<ProposalRow[]>(`plans:${plan.id}:proposals`, () =>
    api.get<ProposalRow[]>(`/plans/${plan.id}/proposals`),
  );
  const space = plan.design_space ?? {};
  const bounds = Object.entries(space.bounds ?? {});
  const editable = plan.state === 'draft' && plan.approval_state !== 'approved' && can('plan.edit');
  return (
    <div className="grid cols-2">
      <Panel
        title={`闭环实验 · 第 ${plan.round_no ?? 1} 轮`}
        aside={
          <button
            className="btn sm"
            onClick={() =>
              api.download(`/plans/${plan.id}/dataset.csv`, `${plan.id}-dataset.csv`).catch((e) => toast.push(e.message))
            }
          >
            导出训练数据
          </button>
        }
      >
        {plan.parent_plan_id ? (
          <div className="small">
            由 <Link to={`/plans/${plan.parent_plan_id}`}>{plan.parent_plan_id}</Link> 的提案生成
          </div>
        ) : null}
        {plan.design_points?.length ? (
          <div className="small muted">本方案条件为 {plan.design_points.length} 个显式设计点（不做全因子组合）</div>
        ) : null}
        <div className="small" style={{ marginTop: 6 }}>
          <b>设计空间</b>
          {bounds.length ? (
            <ul className="tight">
              {bounds.map(([name, bound]) => (
                <li key={name}>
                  {name}：{bound.min ?? '−∞'} … {bound.max ?? '+∞'}
                </li>
              ))}
              {(space.forbidden ?? []).map((rule, index) => (
                <li key={`f${index}`} className="bad-text">
                  禁止：{Object.entries(rule).map(([k, v]) => `${k}=${v}`).join('、')}
                </li>
              ))}
              {space.max_points ? <li>每轮最多 {space.max_points} 个点</li> : null}
            </ul>
          ) : (
            <div className="muted">未设置：不能接收外部提案</div>
          )}
        </div>
        {editable ? (
          <button className="btn sm" onClick={() => setEditingSpace(true)}>
            编辑设计空间
          </button>
        ) : (
          <div className="tiny muted">设计空间随方案审批冻结；已批准的方案才能接收提案</div>
        )}
        <div className="tiny muted" style={{ marginTop: 6 }}>
          外部优化器用服务身份调用 POST /api/runtime/plans/{plan.id}/proposals（需 plan_proposals 授权）。
          训练数据只含复核通过、质量有效的当前结果版本。
        </div>
      </Panel>
      <Panel title={`收到的提案（${proposals.data?.length ?? 0}）`} flush>
        {proposals.data?.length ? (
          <table>
            <tbody>
              {proposals.data.map((row) => (
                <tr key={row.id}>
                  <td>
                    <span className="mono small">{row.proposal_id}</span>
                    <div className="tiny muted">
                      {row.source || '—'} · {row.model_version || '—'} · {row.points.length} 点
                    </div>
                    {row.issues.length ? <div className="tiny bad-text">{row.issues.slice(0, 3).join('；')}</div> : null}
                  </td>
                  <td>
                    <Pill state={row.state === 'accepted' ? 'approved' : 'rejected'} label={row.state === 'accepted' ? '已接受' : '已拒绝'} />
                  </td>
                  <td className="row-end">
                    {row.created_plan_id ? <Link to={`/plans/${row.created_plan_id}`}>{row.created_plan_id}</Link> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>还没有提案</Empty>
        )}
      </Panel>
      {editingSpace ? (
        <DesignSpaceDialog plan={plan} invalidates={invalidates} onClose={() => setEditingSpace(false)} />
      ) : null}
    </div>
  );
}

function DesignSpaceDialog({
  plan,
  invalidates,
  onClose,
}: {
  plan: PlanDetail;
  invalidates: string[];
  onClose: () => void;
}) {
  const toast = useToast();
  const initial = plan.design_space ?? {};
  const [bounds, setBounds] = useState<Record<string, { min: number | ''; max: number | '' }>>(() =>
    Object.fromEntries(
      plan.factors.map((factor) => {
        const bound = initial.bounds?.[factor.name] ?? {};
        return [factor.name, { min: bound.min ?? '', max: bound.max ?? '' }];
      }),
    ),
  );
  const [maxPoints, setMaxPoints] = useState<number | ''>(initial.max_points ?? '');
  const [forbidden, setForbidden] = useState(() => JSON.stringify(initial.forbidden ?? [], null, 0));
  const [error, setError] = useState('');
  const save = useMutation((payload: Record<string, unknown>) => api.patch(`/plans/${plan.id}`, payload), {
    invalidates,
    onSuccess: () => {
      toast.push('设计空间已保存；随方案审批冻结');
      onClose();
    },
  });
  const submit = () => {
    let rules: DesignSpace['forbidden'];
    try {
      rules = JSON.parse(forbidden || '[]');
      if (!Array.isArray(rules)) throw new Error();
    } catch {
      setError('禁止组合必须是 JSON 数组，如 [{"FEC 含量": 10, "注液量": 40}]');
      return;
    }
    const design_space: DesignSpace = {
      bounds: Object.fromEntries(
        Object.entries(bounds).map(([name, bound]) => [
          name,
          { min: bound.min === '' ? null : bound.min, max: bound.max === '' ? null : bound.max },
        ]),
      ),
      forbidden: rules,
      ...(maxPoints === '' ? {} : { max_points: maxPoints }),
    };
    save.run({ design_space, row_version: plan.row_version }).catch((e) => setError(e.message));
  };
  return (
    <Modal
      title={`设计空间 · ${plan.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={save.pending} onClick={submit}>
            保存
          </button>
        </>
      }
    >
      <div className="note">外部优化器的提案必须落在这里的边界内、且不命中禁止组合；超出的整份提案被拒绝并留档。</div>
      {plan.factors.map((factor) => (
        <div className="grid cols-3" key={factor.name}>
          <Field label="因子">
            <input readOnly value={`${factor.name}${factor.unit ? `（${factor.unit.trim()}）` : ''}`} />
          </Field>
          <Field label="下限">
            <NumberInput
              value={bounds[factor.name]?.min ?? ''}
              ariaLabel={`${factor.name} 下限`}
              onChange={(next) => setBounds((current) => ({ ...current, [factor.name]: { ...current[factor.name], min: next } }))}
            />
          </Field>
          <Field label="上限">
            <NumberInput
              value={bounds[factor.name]?.max ?? ''}
              ariaLabel={`${factor.name} 上限`}
              onChange={(next) => setBounds((current) => ({ ...current, [factor.name]: { ...current[factor.name], max: next } }))}
            />
          </Field>
        </div>
      ))}
      <Field label="每轮最多设计点数（可选）">
        <NumberInput value={maxPoints} ariaLabel="最多设计点数" onChange={(next) => setMaxPoints(next)} />
      </Field>
      <Field label="禁止组合（JSON 数组）" hint='如 [{"FEC 含量": 10, "注液量": 40}]'>
        <textarea rows={2} className="mono" value={forbidden} onChange={(event) => setForbidden(event.target.value)} />
      </Field>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/* 结构化因子编辑。因子 × 水平决定条件矩阵，所以这里改一个值，右侧矩阵与孔位预览随之重算；
   矩阵锁定后不可编辑，避免在途批次的条件定义被改写。 */
function FactorEditor({
  plan,
  onClose,
  invalidates,
}: {
  plan: PlanDetail;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const lots = useQuery<LotRow[]>('lots', () => api.get<LotRow[]>('/lots'));
  const [factors, setFactors] = useState<Factor[]>(() => JSON.parse(JSON.stringify(plan.factors ?? [])));
  const [control, setControl] = useState<(number | string)[] | null>(() => plan.control?.cond ?? null);
  const [repeats, setRepeats] = useState<number | ''>(plan.repeats);
  const [layout, setLayout] = useState(plan.layout);
  const [seed, setSeed] = useState<number | ''>(plan.seed);

  const materials = [...new Set((lots.data ?? []).map((lot) => lot.material))].sort();
  const unitOf = (material: string) => (lots.data ?? []).find((lot) => lot.material === material)?.unit ?? '';

  const save = useMutation(
    (payload: Record<string, unknown>) => api.patch(`/plans/${plan.id}`, payload),
    {
      invalidates,
      onSuccess: () => {
        toast.push('因子结构已更新，条件矩阵已重算');
        onClose();
      },
    },
  );

  const update = (index: number, change: Partial<Factor>) =>
    setFactors((current) => current.map((factor, order) => (order === index ? { ...factor, ...change } : factor)));

  const setLevel = (index: number, position: number, raw: string) => {
    const value: number | string = raw.trim() !== '' && Number.isFinite(Number(raw)) ? Number(raw) : raw;
    update(index, { levels: factors[index].levels.map((level, order) => (order === position ? value : level)) });
  };

  /* 对照按「每个因子选一个水平」表达，因子数量变了就作废，避免维度对不上。 */
  const controlValid = control !== null && control.length === factors.length;
  const controlLabel = controlValid
    ? factors.map((factor, index) => `${factor.name} ${control[index]}${factor.unit}`).join(' · ')
    : '';

  const submit = () => {
    const cleaned = factors
      .filter((factor) => factor.name.trim() && factor.levels.length)
      .map((factor) => ({
        ...factor,
        name: factor.name.trim(),
        levels: factor.levels.filter((level) => level !== ''),
      }));
    save
      .run({
        factors: cleaned,
        control: controlValid ? { label: controlLabel, cond: control } : null,
        repeats: repeats === '' ? 1 : repeats,
        layout,
        seed: seed === '' ? 1 : seed,
      })
      .catch((caught) => toast.push(caught.message));
  };

  return (
    <Modal
      title="编辑因子与水平"
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={save.pending} onClick={submit}>
            保存
          </button>
        </>
      }
    >
      <div className="note">
        条件矩阵 = 各因子水平的全组合 × 重复次数。矩阵锁定后不可修改因子结构；
        带物料换算的因子会进入计划页的物料需求预览，对应批号未放行时开跑检查会拦截。
      </div>

      <div className="grid cols-3">
        <Field label="重复次数">
          <NumberInput value={repeats} invalid={!(Number(repeats) >= 1 && Number(repeats) <= 12)} onChange={setRepeats} />
        </Field>
        <Field label="孔位布局">
          <select value={layout} onChange={(event) => setLayout(event.target.value)}>
            <option value="sequential">顺序</option>
            <option value="randomized">随机化</option>
          </select>
        </Field>
        <Field label="随机种子" hint="同一种子重放出同一张孔位图">
          <NumberInput value={seed} disabled={layout !== 'randomized'} onChange={setSeed} />
        </Field>
      </div>

      <div>
        <div className="small muted" style={{ marginBottom: 6 }}>
          因子与水平
        </div>
        <div className="grid" style={{ gap: 10 }}>
          {factors.map((factor, index) => (
            <div key={index} className="note" style={{ display: 'grid', gap: 8 }}>
              <div className="grid cols-3">
                <Field label="因子名称">
                  <input value={factor.name} onChange={(event) => update(index, { name: event.target.value })} />
                </Field>
                <Field label="单位" hint="拼在水平值后面显示">
                  <input value={factor.unit ?? ''} onChange={(event) => update(index, { unit: event.target.value })} />
                </Field>
                <div className="row-end" style={{ alignItems: 'end' }}>
                  <button
                    className="btn sm danger"
                    onClick={() => {
                      setFactors((current) => current.filter((_, order) => order !== index));
                      setControl(null);
                    }}
                  >
                    删除因子
                  </button>
                </div>
              </div>

              <Field label={`水平（${factor.levels.length} 个）`}>
                <div className="row">
                  {factor.levels.map((level, position) => (
                    <span key={position} className="row" style={{ gap: 2 }}>
                      <input
                        className="mono"
                        style={{ width: 84 }}
                        value={String(level)}
                        aria-label={`${factor.name} 水平 ${position + 1}`}
                        onChange={(event) => setLevel(index, position, event.target.value)}
                      />
                      <button
                        className="btn sm"
                        aria-label={`删除水平 ${position + 1}`}
                        onClick={() =>
                          update(index, { levels: factor.levels.filter((_, order) => order !== position) })
                        }
                      >
                        ×
                      </button>
                    </span>
                  ))}
                  <button className="btn sm" onClick={() => update(index, { levels: [...factor.levels, ''] })}>
                    添加水平
                  </button>
                </div>
              </Field>

              <Field
                label="作用于设备参数（可选）"
                hint="选了就按孔位把该因子的水平写进这一步的设备指令；不选则条件只区分样本，设备按流程固定参数执行"
              >
                <select
                  value={factor.target ? `${factor.target.step_id}|${factor.target.param}` : ''}
                  onChange={(event) => {
                    const [stepId, param] = event.target.value.split('|');
                    update(index, { target: event.target.value ? { step_id: stepId, param } : undefined });
                  }}
                >
                  <option value="">不作用于设备（仅区分样本）</option>
                  {(plan.target_options ?? []).map((option) =>
                    option.params.map((param) => (
                      <option key={`${option.step_id}|${param.name}`} value={`${option.step_id}|${param.name}`}>
                        {option.step_name} · {param.name}
                        {param.unit ? `（${param.unit}）` : ''}
                      </option>
                    )),
                  )}
                </select>
              </Field>

              <div className="grid cols-3">
                <Field label="物料换算（可选）">
                  <select
                    value={factor.material?.name ?? ''}
                    onChange={(event) =>
                      update(index, {
                        material: event.target.value
                          ? { name: event.target.value, unit: unitOf(event.target.value), per: factor.material?.per ?? 1 }
                          : undefined,
                      })
                    }
                  >
                    <option value="">不换算</option>
                    {materials.map((material) => (
                      <option key={material} value={material}>
                        {material}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="每单位水平用量">
                  <NumberInput
                    value={factor.material?.per ?? ''}
                    disabled={!factor.material}
                    ariaLabel="每单位水平用量"
                    onChange={(next) =>
                      factor.material &&
                      update(index, { material: { ...factor.material, per: next === '' ? 0 : next } })
                    }
                  />
                </Field>
                <Field label="物料单位">
                  <input readOnly className="mono" value={factor.material?.unit ?? ''} />
                </Field>
              </div>
            </div>
          ))}
        </div>
        <button
          className="btn sm"
          style={{ marginTop: 8 }}
          onClick={() => setFactors((current) => [...current, { name: '', unit: '', levels: [''] }])}
        >
          添加因子
        </button>
      </div>

      <div>
        <div className="small muted" style={{ marginBottom: 6 }}>
          对照条件（每个因子选一个水平；对照必须落在矩阵内，否则锁定校验不通过）
        </div>
        <div className="row">
          <label className="check">
            <input type="checkbox" checked={control === null} onChange={() => setControl(null)} />
            无对照
          </label>
          {factors.map((factor, index) => (
            <Field key={index} label={factor.name || `因子 ${index + 1}`}>
              <select
                value={String(control?.[index] ?? '')}
                onChange={(event) => {
                  const picked = factor.levels.find((level) => String(level) === event.target.value);
                  const next = [...(control ?? factors.map((f) => f.levels[0] ?? ''))];
                  next.length = factors.length;
                  next[index] = picked ?? '';
                  setControl(next);
                }}
              >
                <option value="">选择水平</option>
                {factor.levels.map((level, position) => (
                  <option key={position} value={String(level)}>
                    {String(level)}
                    {factor.unit}
                  </option>
                ))}
              </select>
            </Field>
          ))}
        </div>
        {controlValid ? <div className="small muted">对照：{controlLabel}</div> : null}
      </div>
    </Modal>
  );
}
