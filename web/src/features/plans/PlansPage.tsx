import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { PlanTemplateRow, MetricRow, PlanSummary, RecipeSummary } from '../../shared/types';
import { ConfirmDialog, Empty, Field, Modal, Panel, Pill, useToast } from '../../shared/ui';

export function PlansPage() {
  const { can } = useSession();
  const toast = useToast();
  const plans = useQuery<PlanSummary[]>('plans', () => api.get<PlanSummary[]>('/plans'));
  const recipes = useQuery<RecipeSummary[]>('recipes', () => api.get<RecipeSummary[]>('/recipes'));
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<PlanSummary | null>(null);
  const [typeFilter, setTypeFilter] = useState('');
  const metrics = useQuery<MetricRow[]>('metrics', () => api.get<MetricRow[]>('/metrics?only_active=true'));
  const [form, setForm] = useState({
    name: '', recipe_id: '', plan_type: 'matrix', goal: '', repeats: 2,
    layout: 'randomized', seed: 20260920, sample_count: 4,
  });
  const [requiredMetrics, setRequiredMetrics] = useState<string[]>([]);
  const [templateId, setTemplateId] = useState('');
  const templates = useQuery<PlanTemplateRow[]>('plans:templates', () => api.get<PlanTemplateRow[]>('/plans/templates'));
  const template = templates.data?.find((row) => row.id === templateId);

  const create = useMutation(
    () =>
      templateId
        // 套用模板：只给名称与（可选的）流程，其余结构取自模板
        ? api.post<PlanSummary>('/plans', { name: form.name, template_id: templateId, ...(form.recipe_id ? { recipe_id: form.recipe_id } : {}) })
        : api.post<PlanSummary>('/plans', {
        ...form,
        // 非矩阵方案不带因子；样本数只对单条件方案有意义
        factors: [],
        control: null,
        sample_count: form.plan_type === 'single_condition' ? form.sample_count : 0,
        required_metrics: requiredMetrics,
      }),
    {
      invalidates: ['plans', 'dashboard'],
      onSuccess: (plan) => {
        toast.push(
          plan.plan_type === 'matrix'
            ? `${plan.id} 已创建，去详情页定义因子与水平`
            : `${plan.id} 已创建，去详情页补齐样本与指标后提交评审`,
        );
        setCreating(false);
      },
    },
  );

  const remove = useMutation((planId: string) => api.remove(`/plans/${planId}`), {
    invalidates: ['plans', 'dashboard', 'audit'],
    onSuccess: () => {
      toast.push('计划草稿已删除');
      setDeleting(null);
    },
  });

  return (
    <div className="page">
      <div className="page-head">
        <h1>实验方案</h1>
        <span className="small muted">
          三种类型：矩阵实验、单条件样本实验、委托检测。校验按类型分支，非矩阵方案不要求因子矩阵。
        </span>
        {can('plan.edit') ? (
          <button
            className="btn primary"
            onClick={() => {
              setForm((current) => ({ ...current, recipe_id: recipes.data?.[0]?.id ?? '' }));
              setCreating(true);
            }}
          >
            新建方案
          </button>
        ) : null}
      </div>

      <Panel
        title={`方案（${(plans.data ?? []).filter((row) => !typeFilter || row.plan_type === typeFilter).length}）`}
        aside={
          <div className="filters">
            <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>
              <option value="">全部类型</option>
              <option value="matrix">矩阵实验</option>
              <option value="single_condition">单条件样本实验</option>
              <option value="commissioned_test">委托检测</option>
            </select>
          </div>
        }
        flush
      >
        {plans.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>方案</th>
                <th>类型</th>
                <th>流程</th>
                <th>结构</th>
                <th>审批</th>
                <th className="num">样本</th>
                <th>已生成批次</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {plans.data
                .filter((row) => !typeFilter || row.plan_type === typeFilter)
                .map((plan) => (
                <tr key={plan.id}>
                  <td>
                    <Link to={`/plans/${plan.id}`}>{plan.name}</Link>
                    <div className="tiny muted mono">{plan.id} · {plan.owner} · {plan.created}</div>
                  </td>
                  <td className="small">
                    <span className="tag">{plan.plan_type_label}</span>
                  </td>
                  <td className="mono">{plan.recipe_id}</td>
                  <td>
                    <Pill state={plan.state} label={plan.state_label} />
                  </td>
                  <td>
                    <Pill state={plan.approval_state} label={plan.approval_label} />
                    {plan.approved_version ? (
                      <div className="tiny muted">已批准 v{plan.approved_version}</div>
                    ) : null}
                  </td>
                  <td className="num">
                    {plan.is_matrix ? `${plan.condition_count} × ${plan.repeats} = ` : ''}
                    {plan.sample_count}
                  </td>
                  <td className="small mono">{plan.batches.join('、') || '—'}</td>
                  <td>
                    <Link className="btn sm" to={`/plans/${plan.id}`}>
                      详情
                    </Link>
                    {can('plan.edit') ? (
                      <button
                        className="btn sm danger"
                        disabled={plan.delete_blockers.length > 0}
                        title={plan.delete_blockers.join('；') || undefined}
                        onClick={() => setDeleting(plan)}
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
          <Empty>还没有实验方案</Empty>
        )}
      </Panel>

      {deleting ? (
        <ConfirmDialog
          title={`删除实验方案 · ${deleting.id}`}
          danger
          confirmLabel="删除"
          pending={remove.pending}
          error={remove.error?.message}
          onClose={() => setDeleting(null)}
          onConfirm={() => remove.run(deleting.id).catch(() => undefined)}
        >
          <div className="note warn">
            将删除「{deleting.name}」及其结构定义（{deleting.sample_count} 个样本）。流程不受影响。
            已批准、已绑定批次或已有任务的方案不能删除。
          </div>
        </ConfirmDialog>
      ) : null}

      {creating ? (
        <Modal
          title="新建实验方案"
          onClose={() => setCreating(false)}
          footer={
            <>
              <button className="btn" onClick={() => setCreating(false)}>
                取消
              </button>
              <button
                className="btn primary"
                disabled={!form.name || !form.recipe_id || create.pending}
                onClick={() => create.run().catch((error) => toast.push(error.message))}
              >
                创建草稿
              </button>
            </>
          }
        >
          <Field label="从模板创建" hint={template ? `${template.plan_type_label} · ${template.description || '取模板里的因子、重复、布局、指标与资源需求'}` : '不选就是空白方案'}>
            <select
              value={templateId}
              onChange={(event) => {
                setTemplateId(event.target.value);
                const picked = templates.data?.find((row) => row.id === event.target.value);
                if (picked?.recipe_id) setForm({ ...form, recipe_id: picked.recipe_id });
              }}
            >
              <option value="">不用模板</option>
              {(templates.data ?? []).map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="名称">
            <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
          </Field>
          {templateId ? null : (<>
          <Field label="方案类型" hint="类型决定校验分支：非矩阵方案不要求因子与水平">
            <select value={form.plan_type} onChange={(event) => setForm({ ...form, plan_type: event.target.value })}>
              <option value="matrix">矩阵实验</option>
              <option value="single_condition">单条件样本实验</option>
              <option value="commissioned_test">委托检测</option>
            </select>
          </Field>
          <Field label="所需检测指标" hint="方案批准前必须指定；建检测任务时会冻结这份集合">
            <select
              multiple
              size={4}
              value={requiredMetrics}
              onChange={(event) =>
                setRequiredMetrics(Array.from(event.target.selectedOptions).map((option) => option.value))
              }
            >
              {(metrics.data ?? []).map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name} {row.version}
                  {row.unit ? `（${row.unit}）` : ''}
                  {row.numeric ? '' : ' · 非数值'}
                </option>
              ))}
            </select>
          </Field>
          {form.plan_type === 'single_condition' ? (
            <Field label="样本数" hint="也可以在详情页改成显式样本清单">
              <input
                type="number"
                min={1}
                max={96}
                value={form.sample_count}
                onChange={(event) => setForm({ ...form, sample_count: Number(event.target.value) })}
              />
            </Field>
          ) : null}
          </>)}
          <Field label="实验流程" hint="样品位数由流程决定，样本数不能超过它">
            <select value={form.recipe_id} onChange={(event) => setForm({ ...form, recipe_id: event.target.value })}>
              {(recipes.data ?? []).map((recipe) => (
                <option key={recipe.id} value={recipe.id}>
                  {recipe.id} · {recipe.name}（每批 {recipe.plate} 位）
                </option>
              ))}
            </select>
          </Field>
          {templateId ? null : (<>
          <Field label="研究目标">
            <textarea rows={2} value={form.goal} onChange={(event) => setForm({ ...form, goal: event.target.value })} />
          </Field>
          <div className="grid cols-3" style={{ display: form.plan_type === 'matrix' ? undefined : 'none' }}>
            <Field label="重复次数">
              <input
                type="number"
                min={1}
                max={12}
                value={form.repeats}
                onChange={(event) => setForm({ ...form, repeats: Number(event.target.value) })}
              />
            </Field>
            <Field label="孔位布局">
              <select value={form.layout} onChange={(event) => setForm({ ...form, layout: event.target.value })}>
                <option value="randomized">随机化</option>
                <option value="sequential">顺序</option>
              </select>
            </Field>
            <Field label="随机种子">
              <input
                type="number"
                value={form.seed}
                onChange={(event) => setForm({ ...form, seed: Number(event.target.value) })}
              />
            </Field>
          </div>
          </>)}
          {create.error ? <div className="note bad">{create.error.message}</div> : null}
        </Modal>
      ) : null}
    </div>
  );
}
