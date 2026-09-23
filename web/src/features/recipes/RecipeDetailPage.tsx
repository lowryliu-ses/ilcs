import { Link, useNavigate, useParams } from 'react-router-dom';

import { api } from '../../shared/api';
import { params as formatParams } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { RecipeDetail } from '../../shared/types';
import { CheckList, Empty, Panel, Pill, useToast } from '../../shared/ui';
import { useSignature } from '../../shared/signature';

export function RecipeDetailPage() {
  const { recipeId = '' } = useParams();
  const navigate = useNavigate();
  const { can } = useSession();
  const { sign } = useSignature();
  const toast = useToast();
  const recipe = useQuery<RecipeDetail>(`recipes:${recipeId}`, () => api.get<RecipeDetail>(`/recipes/${recipeId}`));
  const invalidates = [`recipes:${recipeId}`, 'recipes', 'dashboard', 'audit'];

  const submit = useMutation(() => api.post(`/recipes/${recipeId}/submit`), {
    invalidates,
    onSuccess: () => toast.push('已提交评审，服务端能力校验通过'),
  });
  const transition = useMutation(
    (payload: { target_state: string; signature_id: string }) => api.post(`/recipes/${recipeId}/transition`, payload),
    { invalidates, onSuccess: () => toast.push('状态已流转并写入审计') },
  );
  const revision = useMutation(() => api.post<RecipeDetail>(`/recipes/${recipeId}/revision`), {
    invalidates,
    onSuccess: (created) => {
      toast.push(`${created.id} 修订草稿已创建`);
      navigate(`/recipes/${created.id}/edit`);
    },
  });

  if (!recipe.data) return <div className="boot">{recipe.error ? recipe.error.message : '加载中…'}</div>;
  const data = recipe.data;

  const runTransition = async (target: string, label: string, meanings: string[]) => {
    // 签名绑定方法 ID 与版本：服务端按严格模式核对，挪用到别的方法或旧版本会被拒
    const signatureId = await sign(label, data.id, meanings, data.row_version);
    if (!signatureId) return;
    await transition.run({ target_state: target, signature_id: signatureId }).catch((error) => toast.push(error.message));
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>
            {data.name} <Pill state={data.state} label={data.state_label} />
            {data.needs_revision ? <Pill state="fault" label="需修订" /> : null}
          </h1>
          <div className="small muted">
            <span className="mono">
              {data.id} v{data.version}
            </span>{' '}
            · 每批 {data.plate} 位 · 风险评估 {data.risk || '缺失'} · 黄金批次 {data.golden_batch_id || '—'}
          </div>
        </div>
        <div className="row">
          {data.state === 'draft' && can('recipe.edit') ? (
            <Link className="btn" to={`/recipes/${data.id}/edit`}>
              图形化编辑
            </Link>
          ) : null}
          {data.state === 'draft' && can('recipe.submit') ? (
            <button
              className="btn primary"
              disabled={!data.valid || submit.pending}
              title={data.valid ? undefined : '能力校验未通过'}
              onClick={() => submit.run().catch((error) => toast.push(error.message))}
            >
              提交评审
            </button>
          ) : null}
          {data.state === 'review' && can('recipe.approve') ? (
            <button className="btn primary" onClick={() => runTransition('approved', '批准配方', ['评审通过'])}>
              批准
            </button>
          ) : null}
          {data.state === 'approved' && can('recipe.release') ? (
            <button className="btn primary" onClick={() => runTransition('released', '发布配方', ['批准发布'])}>
              发布
            </button>
          ) : null}
          {data.state === 'released' && can('recipe.edit') ? (
            <button className="btn" onClick={() => revision.run().catch((error) => toast.push(error.message))}>
              新建修订草稿
            </button>
          ) : null}
          {data.state === 'released' && can('recipe.release') ? (
            <button className="btn danger" onClick={() => runTransition('retired', '退役配方', ['不再使用'])}>
              退役
            </button>
          ) : null}
        </div>
      </div>

      {data.needs_revision ? (
        <div className="banner bad">
          工位能力极限变更后重校验未通过，本版本已进入需修订，不能再创建新批次。已在途批次按快照继续执行。
        </div>
      ) : null}

      <div className="grid cols-2-1">
        <Panel title="步骤与工位极限对照" flush>
          <table>
          <thead>
            <tr>
              <th>步骤</th>
              <th>能力</th>
              <th>参数</th>
              <th className="num">时长</th>
              <th>可承接工位</th>
              <th>恢复规则（能力继承）</th>
            </tr>
          </thead>
          <tbody>
            {data.validation.map((step) => {
              const recovery = data.recovery_by_capability[step.cap] ?? {};
              return (
                <tr key={step.index}>
                  <td>
                    {step.index + 1}. {step.name}
                    {data.steps[step.index]?.hard?.maxGapMin ? (
                      <div className="tiny warn-text">硬时限 {data.steps[step.index].hard?.maxGapMin} min</div>
                    ) : null}
                  </td>
                  <td>
                    {step.cap_name}
                    <div className="tiny muted mono">{step.cap}</div>
                  </td>
                  <td className="mono small">{formatParams(step.params)}</td>
                  <td className="num">{step.dur} min</td>
                  <td className="small">
                    {step.ok ? (
                      <span className="mono">{step.fits.join('、')}</span>
                    ) : (
                      <span className="bad-text">{step.blockers.join('；') || '无可承接工位'}</span>
                    )}
                  </td>
                  <td className="small muted">
                    {recovery.pausable ? `可保持 ≤ ${recovery.maxHoldMin} min` : '不可保持'} ·{' '}
                    {recovery.retryable ? '可重试' : '不可重试'}
                    {recovery.verify?.length ? <div className="tiny">恢复前核实：{recovery.verify.join('、')}</div> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
          </table>
        </Panel>

        <Panel title="校验">
          <CheckList checks={data.checks} />
          <div className="small muted">
            前五项通过才能提交评审。判据由服务端计算，与图形化编辑器里的即时提示同源。
          </div>
        </Panel>
      </div>

      <div className="grid cols-2">
        <Panel title="物料需求 BOM" flush>
          <table>
            <thead>
              <tr>
                <th>物料</th>
                <th className="num">每批需求</th>
              </tr>
            </thead>
            <tbody>
              {data.bom.map((item) => (
                <tr key={item.material}>
                  <td>{item.material}</td>
                  <td className="num">
                    {item.qty} {item.unit}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="关联实验计划" flush>
          {data.plans.length ? (
            <table>
              <thead>
                <tr>
                  <th>计划</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {data.plans.map((plan) => (
                  <tr key={plan.id}>
                    <td>
                      <Link to={`/plans/${plan.id}`}>{plan.name}</Link>
                      <div className="tiny muted mono">{plan.id}</div>
                    </td>
                    <td>
                      <Pill state={plan.state} label={plan.state === 'locked' ? '矩阵已锁定' : '草稿'} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>没有引用该配方的实验计划</Empty>
          )}
        </Panel>
      </div>

      <div className="grid cols-2">
        <Panel title="版本历史" flush>
          <table>
            <thead>
              <tr>
                <th>版本</th>
                <th>动作</th>
                <th>操作者</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {data.history.map((entry, index) => (
                <tr key={`${entry.v}-${index}`}>
                  <td className="mono">v{entry.v}</td>
                  <td>{entry.note}</td>
                  <td>{entry.by}</td>
                  <td className="small mono">{entry.at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="版本差异">
          {data.diff.length ? (
            <pre className="diff">
              {data.diff.map(([sign, line], index) => (
                <div key={index} className={sign === '+' ? 'add' : 'del'}>
                  {sign} {line}
                </div>
              ))}
            </pre>
          ) : (
            <Empty>没有记录差异</Empty>
          )}
        </Panel>
      </div>

    </div>
  );
}
