import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api } from '../../shared/api';
import { num } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { CommentsPanel } from '../../shared/comments';
import type {
  AnalysisRunRow, ApprovalLevel, DatasetSnapshotRow, DesignSpace, DiffRow, Factor, LotRow, MetricRow, PlanDetail, ProposalRow,
} from '../../shared/types';
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
            对照 {data.control?.label ?? '未设'} ·{' '}
            {data.sample_ids.length
              ? `指定物理样本 ${data.sample_ids.length} 个（哪个样本对哪个条件见条件矩阵）${
                data.sample_policy === 'continue' ? '，接着用上一步的产物' : '，样本只用一次'}`
              : '未指定物理样本：每个运行登记新样本'}
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

      {data.batch_plan && (data.batch_plan.split || data.batch_plan.error) ? (
        <Panel title="分批执行">
          <div className={`small ${data.batch_plan.error ? 'bad-text' : ''}`}>{data.batch_plan.detail}</div>
          {data.batch_plan.split ? (
            <div className="tiny muted">
              容量是每一批的约束：方案照常审批，建立实验任务时按这个分法拆成 {data.batch_plan.batches} 个子任务，
              每个子任务一个批次；父任务按样本汇总进度，出一份合并报告。
              {data.is_matrix ? '矩阵方案按重复分批，每批都包含全部条件，批内再随机排布。' : ''}
            </div>
          ) : null}
        </Panel>
      ) : null}

      {data.is_matrix ? (
      <Panel title={`条件矩阵（${data.conditions.length} 组，${data.sample_count} 样品）`} flush>
        <table>
          <thead>
            <tr>
              <th>条件组</th>
              <th>水平组合</th>
              <th>{data.batch_plan?.split ? `孔位（第 1 批，共 ${data.batch_plan.batches} 批）` : '孔位'}</th>
              {data.sample_ids.length ? <th>指定样本</th> : null}
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
                {data.sample_ids.length ? (
                  <td className="mono small">{samplesOf(data.sample_ids, data.repeats, condition.group) || '—'}</td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      ) : null}

      {data.materials.length ? (
      <Panel title={data.batch_plan?.split ? `物料需求预览（${data.batch_plan.batches} 批合计）` : '物料需求预览（每批）'} flush>
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
          创建批次时写入预留：流程 BOM 列出的物料按 BOM；BOM 没列、但有步骤声明投料的物料，按本批分到的样本的因子水平 ×
          换算量预留。已放行且在有效期内的可用量不足时，批次创建失败并整体回滚。其余因子换算物料只作估算，不预留，也不在开跑时检查。
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

/** 矩阵方案指定的物理样本里，某个条件组分到哪几个样本。与后端 batch_service._generate_samples 同一规则：
    第 i 个样本对应「条件序号 × 重复数 + (重复号 − 1)」，条件序号取组名里的数字（C01 → 1），不按行的位置，
    这样页面上看到的对应关系就是建批次时实际分配的那个。 */
function samplesOf(sampleIds: string[], repeats: number, group: string): string {
  const number = Number(group.slice(1));
  if (!/^\d+$/.test(group.slice(1)) || number < 1) return '';
  const per = Math.max(1, Math.trunc(repeats || 1));
  const picked = sampleIds.slice((number - 1) * per, number * per);
  return per === 1 ? picked.join('') : picked.map((id, at) => `重复${at + 1} ${id}`).join(' · ');
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
  const runs = useQuery<AnalysisRunRow[]>(`plans:${plan.id}:analysis-runs`, () =>
    api.get<AnalysisRunRow[]>(`/plans/${plan.id}/analysis-runs`),
  );
  const runById = Object.fromEntries((runs.data ?? []).map((row) => [row.id, row]));
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
            title="只是看一眼此刻的数据；训练请固化快照，结果以后被更正也不影响按快照导出的内容"
            onClick={() =>
              api.download(`/plans/${plan.id}/dataset.csv`, `${plan.id}-dataset.csv`).catch((e) => toast.push(e.message))
            }
          >
            预览当前数据
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
                  {name}：{bound.options ? `允许 ${bound.options.map(String).join('、')}` : `${bound.min ?? '−∞'} … ${bound.max ?? '+∞'}`}
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
          外部优化器用服务身份先固化数据集快照（POST /api/runtime/plans/{plan.id}/datasets）、登记分析运行
          （…/analysis-runs），再提交提案（…/proposals，带 analysis_run_id），均需 plan_proposals 授权。
          训练数据只含复核通过、质量有效的当前结果版本。
        </div>
      </Panel>
      <DatasetPanel plan={plan} />
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
                    {row.analysis_run_id ? (
                      <div className="tiny">
                        分析运行 {runById[row.analysis_run_id]?.run_id ?? row.analysis_run_id.slice(0, 8)}
                        {runById[row.analysis_run_id]
                          ? `（${runById[row.analysis_run_id].program || '—'} ${runById[row.analysis_run_id].program_version}，`
                            + `随机种子 ${runById[row.analysis_run_id].seed || '—'}，快照 ${runById[row.analysis_run_id].snapshot_id.slice(0, 8)}）`
                          : ''}
                      </div>
                    ) : (
                      <div className="tiny muted">未登记分析运行：说不清用了哪份数据</div>
                    )}
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

/* 训练数据快照：固化纳入的结果版本清单、排除清单、数据行与原始文件摘要。之后结果被更正、退回或改判，
   按快照导出的内容都不变；列表里如实标出每份快照有几条后来变了。 */
function DatasetPanel({ plan }: { plan: PlanDetail }) {
  const { can } = useSession();
  const toast = useToast();
  const [note, setNote] = useState('');
  const snapshots = useQuery<DatasetSnapshotRow[]>(`plans:${plan.id}:datasets`, () =>
    api.get<DatasetSnapshotRow[]>(`/plans/${plan.id}/datasets`),
  );
  const create = useMutation(
    () => api.post<DatasetSnapshotRow>(`/plans/${plan.id}/datasets`, { note }),
    {
      invalidates: [`plans:${plan.id}:datasets`, 'audit'],
      onSuccess: (row) => {
        setNote('');
        toast.push(`已固化快照：${row.row_count} 条正式结果、${row.excluded_count} 条排除`);
      },
    },
  );
  return (
    <Panel
      title={`训练数据快照（${snapshots.data?.length ?? 0}）`}
      aside={
        can('plan.edit') ? (
          <button className="btn sm primary" disabled={create.pending} onClick={() => create.run().catch((e) => toast.push(e.message))}>
            固化当前数据
          </button>
        ) : null
      }
      flush
    >
      {can('plan.edit') ? (
        <div style={{ padding: '6px 12px' }}>
          <input value={note} placeholder="备注（可选），如：第 2 轮 BO 训练" onChange={(event) => setNote(event.target.value)} />
        </div>
      ) : null}
      {snapshots.data?.length ? (
        <table>
          <tbody>
            {snapshots.data.map((row) => (
              <tr key={row.id}>
                <td>
                  <span className="mono small">{row.id.slice(0, 8)}</span>
                  <span className="tiny muted"> · 摘要 {row.digest.slice(0, 12)}</span>
                  <div className="tiny muted">
                    {row.created_at.replace('T', ' ')} · {row.row_count} 条正式结果 · {row.excluded_count} 条排除
                    {row.file_count ? ` · ${row.file_count} 个原始文件` : ''}
                    {row.note ? ` · ${row.note}` : ''}
                  </div>
                  {row.changed_count ? (
                    <div className="tiny warn-text">其中 {row.changed_count} 条结果后来被更正、退回或改判；快照内容不变</div>
                  ) : null}
                </td>
                <td className="row-end">
                  <button
                    className="btn sm"
                    onClick={() =>
                      api
                        .download(`/plans/${plan.id}/datasets/${row.id}/export.csv`, `${plan.id}-dataset-${row.id.slice(0, 8)}.csv`)
                        .catch((e) => toast.push(e.message))
                    }
                  >
                    导出
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty>还没有快照：训练前先固化一份，提案才能追到用了哪批数据</Empty>
      )}
    </Panel>
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
  const [bounds, setBounds] = useState<Record<string, { min: number | ''; max: number | ''; options: (number | string)[] }>>(() =>
    Object.fromEntries(
      plan.factors.map((factor) => {
        const bound = initial.bounds?.[factor.name] ?? {};
        // 类别因子缺省允许全部选项
        const options = bound.options ?? categorical(plan, factor) ?? [];
        return [factor.name, { min: bound.min ?? '', max: bound.max ?? '', options }];
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
        plan.factors.map((factor) => {
          const bound = bounds[factor.name];
          return [
            factor.name,
            categorical(plan, factor)
              ? { options: bound.options }
              : { min: bound.min === '' ? null : bound.min, max: bound.max === '' ? null : bound.max },
          ];
        }),
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
      {plan.factors.map((factor) => {
        const choices = categorical(plan, factor);
        if (choices) {
          return (
            <Field key={factor.name} label={`${factor.name}：允许的选项`} hint="提案里这个因子只能取勾选的选项">
              <div className="dep-list">
                {choices.map((option) => (
                  <label key={String(option)} className="check">
                    <input
                      type="checkbox"
                      checked={bounds[factor.name]?.options.includes(option) ?? false}
                      onChange={(event) =>
                        setBounds((current) => {
                          const kept = new Set(current[factor.name]?.options ?? []);
                          if (event.target.checked) kept.add(option);
                          else kept.delete(option);
                          return { ...current, [factor.name]: { ...current[factor.name], options: choices.filter((item) => kept.has(item)) } };
                        })
                      }
                    />
                    {String(option)}
                  </label>
                ))}
              </div>
            </Field>
          );
        }
        return (
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
        );
      })}
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

/** 因子作用的那个设备参数（流程里的步骤 + 参数），没作用于设备或找不到时为 undefined */
function targetParam(plan: PlanDetail, factor: Factor) {
  if (!factor.target) return undefined;
  const option = (plan.target_options ?? []).find((row) => row.step_id === factor.target?.step_id);
  return option?.params.find((param) => param.name === factor.target?.param);
}

/** 类别因子：作用于选项型参数，或水平里有文字（溶剂、催化剂、协议）——设计空间写允许的选项，不写上下限 */
function categorical(plan: PlanDetail, factor: Factor): (number | string)[] | null {
  const param = targetParam(plan, factor);
  if (param?.type === 'enum') return param.options ?? [];
  return factor.levels.some((level) => typeof level === 'string' && level !== '') ? factor.levels.filter((level) => level !== '') : null;
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
  /* 方案指定了物理样本时，样本按「条件顺序 × 重复号」对应；改了重复次数或因子水平，对应关系就变了，
     条数也可能对不上（锁定校验会拦）。给个清除的出口，免得只能重新导入配方表才能再锁定。 */
  const [clearSamples, setClearSamples] = useState(false);
  const [samplePolicy, setSamplePolicy] = useState<'fresh' | 'continue'>(plan.sample_policy ?? 'fresh');
  const structureKey = (list: Factor[]) => JSON.stringify(list.map((factor) => factor.levels));
  /* 已保存的方案本身就对不上（例如之前改重复次数时没勾清除）：照样给出清除的出口，否则只能改回旧值再改 */
  const alreadyMismatched = plan.sample_ids.length > 0 && plan.sample_ids.length !== plan.conditions.length * Math.max(1, plan.repeats);
  /* 有显式设计点（配方表导入的方案）时条件由设计点决定，改因子水平不改变条件，只有重复次数会改变对应 */
  const levelsMatter = !(plan.design_points ?? []).length;
  const mappingChanged =
    plan.sample_ids.length > 0 &&
    (alreadyMismatched ||
      (repeats === '' ? 1 : repeats) !== plan.repeats ||
      (levelsMatter && structureKey(factors) !== structureKey(plan.factors ?? [])));

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
  /* 因子作用的设备参数是选项型时，水平只能从它的选项里挑（勾选），没有单位、也不换算物料 */
  const optionsOf = (factor: Factor): string[] | null => {
    const param = targetParam(plan, factor);
    return param?.type === 'enum' ? param.options ?? [] : null;
  };

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
        sample_policy: samplePolicy,
        ...(mappingChanged && clearSamples ? { sample_ids: [] } : {}),
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
        带物料换算的因子会进入计划页的物料需求预览；如果流程有步骤声明投这种物料、BOM 又没列，
        创建批次时按各样本的水平预留，已放行批号不够就建不了批次。
      </div>

      {mappingChanged ? (
        <div className="note warn">
          这个方案指定了 {plan.sample_ids.length} 个物理样本，按条件顺序 × 重复号逐个对应。改了重复次数或因子水平后，
          哪个样本对哪个条件会变，数量对不上时锁定校验不通过。
          <label className="check" style={{ marginTop: 6 }}>
            <input type="checkbox" checked={clearSamples} onChange={(event) => setClearSamples(event.target.checked)} />
            清除指定的物理样本（之后每个运行登记新样本）
          </label>
        </div>
      ) : null}

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
      {plan.sample_ids.length ? (
        <Field label="指定样本的用法" hint="多步合成：上一批的产物接着做下一步反应时选「接着用」；没跑完的、已处置用尽的样本照样不能进新批次">
          <select value={samplePolicy} onChange={(event) => setSamplePolicy(event.target.value as 'fresh' | 'continue')}>
            <option value="fresh">只用一次（用过的样本不能再进新批次）</option>
            <option value="continue">接着用上一步的产物（允许上一批已跑完的样本）</option>
          </select>
        </Field>
      ) : null}

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
                <Field
                  label="单位"
                  hint={optionsOf(factor) ? '选项型参数没有单位'
                    : 'mmol、eq 按物料登记的换算转成设备单位'}
                >
                  <input
                    value={factor.unit ?? ''}
                    list="factor-units"
                    disabled={Boolean(optionsOf(factor))}
                    onChange={(event) => update(index, { unit: event.target.value, ...(event.target.value.trim() === 'eq' ? {} : { basis: undefined }) })}
                  />
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

              {factor.unit?.trim() === 'eq' ? (
                <div className="grid cols-3">
                  <Field label="当量的基准" hint="限量试剂的物质的量：另一个按 mmol 给的因子，或一个固定的量">
                    <select
                      value={factor.basis?.factor ? `factor:${factor.basis.factor}` : factor.basis?.amount !== undefined ? 'amount' : ''}
                      onChange={(event) => {
                        const value = event.target.value;
                        update(index, {
                          basis: value.startsWith('factor:') ? { factor: value.slice(7) }
                            : value === 'amount' ? { amount: factor.basis?.amount ?? 1, unit: factor.basis?.unit ?? 'mmol' } : undefined,
                        });
                      }}
                    >
                      <option value="">选基准…</option>
                      {factors.filter((other, order) => order !== index && other.name.trim()).map((other) => (
                        <option key={other.name} value={`factor:${other.name}`}>因子「{other.name}」{other.unit ? `（${other.unit}）` : ''}</option>
                      ))}
                      <option value="amount">固定的物质的量</option>
                    </select>
                  </Field>
                  {factor.basis?.amount !== undefined ? (
                    <>
                      <Field label="基准量">
                        <NumberInput value={factor.basis.amount} ariaLabel="当量基准量"
                          onChange={(next) => update(index, { basis: { ...factor.basis, amount: next === '' ? 0 : next } })} />
                      </Field>
                      <Field label="基准量单位">
                        <select value={factor.basis.unit ?? 'mmol'} onChange={(event) => update(index, { basis: { ...factor.basis, unit: event.target.value } })}>
                          {['mmol', 'μmol', 'mol'].map((unit) => <option key={unit} value={unit}>{unit}</option>)}
                        </select>
                      </Field>
                    </>
                  ) : null}
                </div>
              ) : null}

              {optionsOf(factor) ? (
                <Field label={`水平（${factor.levels.length} 个，从参数的选项里勾选）`}>
                  <div className="dep-list">
                    {(optionsOf(factor) ?? []).map((option) => (
                      <label key={option} className="check">
                        <input
                          type="checkbox"
                          checked={factor.levels.includes(option)}
                          onChange={(event) => {
                            const chosen = new Set(factor.levels.filter((level) => event.target.checked || level !== option));
                            if (event.target.checked) chosen.add(option);
                            update(index, { levels: (optionsOf(factor) ?? []).filter((item) => chosen.has(item)) });
                          }}
                        />
                        {option}
                      </label>
                    ))}
                  </div>
                </Field>
              ) : (
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
              )}

              <Field
                label="作用于设备参数（可选）"
                hint="选了就按孔位把该因子的水平写进这一步的设备指令；不选则条件只区分样本，设备按流程固定参数执行"
              >
                <select
                  value={factor.target ? `${factor.target.step_id}|${factor.target.param}` : ''}
                  onChange={(event) => {
                    const [stepId, param] = event.target.value.split('|');
                    const target = event.target.value ? { step_id: stepId, param } : undefined;
                    const picked = targetParam(plan, { ...factor, target });
                    if (picked?.type === 'enum') {
                      // 换到选项型参数：只保留是它选项的水平，单位与物料换算没有意义
                      const options = picked.options ?? [];
                      update(index, {
                        target, unit: '', material: undefined,
                        levels: factor.levels.filter((level): level is string => typeof level === 'string' && options.includes(level)),
                      });
                    } else {
                      update(index, { target });
                    }
                  }}
                >
                  <option value="">不作用于设备（仅区分样本）</option>
                  {(plan.target_options ?? []).map((option) =>
                    option.params.map((param) => {
                      // 说明文字与登记单位分开给；说明里已写了单位就不重复
                      const label = param.label && param.label !== param.name ? param.label : '';
                      const unit = param.unit && !label.includes(param.unit) ? param.unit : '';
                      const note = [label, unit, param.type === 'enum' ? '选项' : ''].filter(Boolean).join('，');
                      return (
                        <option key={`${option.step_id}|${param.name}`} value={`${option.step_id}|${param.name}`}>
                          {option.step_name} · {param.name}
                          {note ? `（${note}）` : ''}
                        </option>
                      );
                    }),
                  )}
                </select>
              </Field>

              <div className="grid cols-3">
                <Field label="物料换算（可选）" hint={optionsOf(factor) ? '选项型参数的水平不是数量，不换算物料' : undefined}>
                  <select
                    value={factor.material?.name ?? ''}
                    disabled={Boolean(optionsOf(factor))}
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
        <datalist id="factor-units">
          {['mmol', 'μmol', 'mol', 'eq', 'mg', 'g', 'μL', 'mL', '℃', 'min', 'h'].map((unit) => <option key={unit} value={unit} />)}
        </datalist>
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
