import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api } from '../../shared/api';
import { num } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { Factor, LotRow, MetricRow, PlanDetail } from '../../shared/types';
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
  const submit = useMutation(() => api.post(`/plans/${planId}/submit`), {
    invalidates,
    onSuccess: () => toast.push('已提交评审；批准后才能建立实验任务与正式批次'),
  });
  const decide = useMutation(
    (payload: { conclusion: string; reason?: string; signature_id?: string }) =>
      api.post(`/plans/${planId}/decision`, payload),
    {
      invalidates,
      onSuccess: (result) => {
        const row = result as PlanDetail;
        toast.push(row.approval_state === 'approved' ? '已批准；该版本冻结' : '已驳回回草稿');
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
  const editable = data.state === 'draft' && data.approval_state !== 'approved' && can('plan.edit');

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
            <span className="mono">{data.id}</span> v{data.version} · 方法{' '}
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
          {data.approval_state === 'draft' && can('plan.submit') ? (
            <button
              className="btn"
              disabled={!data.lockable || submit.pending}
              title={data.lockable ? undefined : '校验未通过，先补齐再提交'}
              onClick={() => submit.run().catch((error) => toast.push(error.message))}
            >
              提交评审
            </button>
          ) : null}
          {data.approval_state === 'review' && can('plan.approve') ? (
            <>
              <button className="btn" onClick={() => setRejecting(true)}>
                驳回
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
                  <td className="small muted">
                    {factor.material ? `${factor.material.name} ${factor.material.per}${factor.material.unit}/单位` : '—'}
                  </td>
                </tr>
              ))}
              {data.factors.length === 0 ? (
                <tr>
                  <td colSpan={3} className="muted">
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
                  <td className="small muted">方法版本</td>
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
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>还没有提交过评审</Empty>
          )}
          <div className="panel-body small muted">
            批准版本不可修改；修订生成新版本，历史运行仍引用原快照。
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
          创建批次时按配方 BOM 写入预留；因子换算物料需先入库并放行，否则开跑检查会拦截。
        </div>
      </Panel>
      ) : (
        <Panel title="物料需求预览">
          <div className="note">该方案没有物料需求（委托检测不强制定义耗材，空 BOM 的方法显示「无需物料」）。</div>
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
            配方 {data.recipe_id} 不受影响。
          </div>
        </ConfirmDialog>
      ) : null}

      {editing ? <FactorEditor plan={data} onClose={() => setEditing(false)} invalidates={invalidates} /> : null}
    </div>
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
