import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { api } from '../../shared/api';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { RecipeSummary } from '../../shared/types';
import { ConfirmDialog, Empty, Field, Modal, Panel, Pill, useToast } from '../../shared/ui';

export function RecipesPage() {
  const { can } = useSession();
  const navigate = useNavigate();
  const toast = useToast();
  const recipes = useQuery<RecipeSummary[]>('recipes', () => api.get<RecipeSummary[]>('/recipes'));
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<RecipeSummary | null>(null);
  const [form, setForm] = useState({ name: '', plate: 24, copy_from: '' });

  const create = useMutation(
    () => api.post<RecipeSummary>('/recipes', { ...form, copy_from: form.copy_from || null }),
    {
      invalidates: ['recipes', 'dashboard'],
      onSuccess: (recipe) => {
        toast.push(`${recipe.id} 草稿已创建，进入图形化编辑`);
        setCreating(false);
        navigate(`/recipes/${recipe.id}/edit`);
      },
    },
  );

  const remove = useMutation((recipeId: string) => api.remove(`/recipes/${recipeId}`), {
    invalidates: ['recipes', 'dashboard', 'audit'],
    onSuccess: () => {
      toast.push('草稿已删除');
      setDeleting(null);
    },
  });

  return (
    <div className="page">
      <div className="page-head">
        <h1>实验流程</h1>
        {can('recipe.edit') ? (
          <button className="btn primary" onClick={() => setCreating(true)}>
            新建草稿
          </button>
        ) : null}
      </div>

      <Panel title={`流程（${recipes.data?.length ?? 0}）`} flush>
        {recipes.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>流程</th>
                <th>版本</th>
                <th>状态</th>
                <th className="num">步骤</th>
                <th>能力校验</th>
                <th>黄金批次</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {recipes.data.map((recipe) => (
                <tr key={recipe.id}>
                  <td>
                    <Link to={`/recipes/${recipe.id}`}>{recipe.name}</Link>
                    <div className="tiny muted mono">
                      {recipe.id}
                      {recipe.parent ? ` · 派生于 ${recipe.parent}` : ''} · {recipe.owner}
                    </div>
                  </td>
                  <td className="mono">v{recipe.version}</td>
                  <td>
                    <Pill state={recipe.state} label={recipe.state_label} />
                    {recipe.needs_revision ? <div className="tiny bad-text">需修订</div> : null}
                  </td>
                  <td className="num">{recipe.step_count}</td>
                  <td>
                    <Pill state={recipe.valid ? 'running' : 'fault'} label={recipe.valid ? '通过' : '未通过'} />
                  </td>
                  <td className="mono small">{recipe.golden_batch_id || '—'}</td>
                  <td className="row-end">
                    {recipe.state === 'draft' && can('recipe.edit') ? (
                      <Link className="btn sm" to={`/recipes/${recipe.id}/edit`}>
                        编辑
                      </Link>
                    ) : null}
                    {can('recipe.edit') ? (
                      <button
                        className="btn sm danger"
                        disabled={recipe.delete_blockers.length > 0}
                        title={recipe.delete_blockers.join('；') || undefined}
                        onClick={() => setDeleting(recipe)}
                      >
                        删除
                      </button>
                    ) : null}
                    <Link className="btn sm" to={`/recipes/${recipe.id}`}>
                      详情
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>还没有实验流程</Empty>
        )}
      </Panel>

      {deleting ? (
        <ConfirmDialog
          title={`删除流程草稿 · ${deleting.id}`}
          danger
          confirmLabel="删除"
          pending={remove.pending}
          error={remove.error?.message}
          onClose={() => setDeleting(null)}
          onConfirm={() => remove.run(deleting.id).catch(() => undefined)}
        >
          <div className="note warn">
            将删除「{deleting.name}」v{deleting.version} 及其 {deleting.step_count} 个步骤。
            删除动作本身写审计；已发布版本与在途批次不受影响，因为它们各有自己的快照。
          </div>
        </ConfirmDialog>
      ) : null}

      {creating ? (
        <Modal
          title="新建流程草稿"
          onClose={() => setCreating(false)}
          footer={
            <>
              <button className="btn" onClick={() => setCreating(false)}>
                取消
              </button>
              <button
                className="btn primary"
                disabled={!form.name || create.pending}
                onClick={() => create.run().catch((error) => toast.push(error.message))}
              >
                创建
              </button>
            </>
          }
        >
          <Field label="名称">
            <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
          </Field>
          <Field label="每批样品位数">
            <input
              type="number"
              min={1}
              max={96}
              value={form.plate}
              onChange={(event) => setForm({ ...form, plate: Number(event.target.value) })}
            />
          </Field>
          <Field label="复制步骤自" hint="可留空，从零开始">
            <select value={form.copy_from} onChange={(event) => setForm({ ...form, copy_from: event.target.value })}>
              <option value="">不复制</option>
              {(recipes.data ?? []).map((recipe) => (
                <option key={recipe.id} value={recipe.id}>
                  {recipe.id} v{recipe.version}
                </option>
              ))}
            </select>
          </Field>
          {create.error ? <div className="note bad">{create.error.message}</div> : null}
        </Modal>
      ) : null}
    </div>
  );
}
