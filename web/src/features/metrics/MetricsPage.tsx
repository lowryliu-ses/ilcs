/* 指标定义。检测任务要求的指标、结果回传认的指标、统计口径引用的指标，都是这里的版本。

   两条规则决定了这一页为什么不是普通的增删改查：
   - 被结果引用过的版本不能改，只能修订出新版本。改了就等于回头篡改已采集数据的口径。
   - 允许范围（min/max、可选值）是**录入校验**，不是质量判定。超范围的值根本进不来；
     进得来的值是否有效，由数据复核的人判定。这两件事在界面上必须说清楚，否则会有人
     把「在范围内」当成「合格」。 */
import { useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { MetricRow } from '../../shared/types';
import {
  ConfirmDialog, Field, ListState, Modal, Panel, Pill, useToast,
} from '../../shared/ui';

const VALUE_TYPES: [MetricRow['value_type'], string][] = [
  ['number', '数值'],
  ['text', '文本'],
  ['enum', '枚举'],
];

const TYPE_LABEL = Object.fromEntries(VALUE_TYPES) as Record<string, string>;

export function MetricsPage() {
  const { can } = useSession();
  const [onlyActive, setOnlyActive] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<MetricRow | null>(null);
  const [revising, setRevising] = useState<MetricRow | null>(null);
  const [retiring, setRetiring] = useState<MetricRow | null>(null);
  const toast = useToast();

  const key = `metrics:${onlyActive}`;
  const metrics = useQuery<MetricRow[]>(key, () =>
    api.get<MetricRow[]>(`/metrics${onlyActive ? '?only_active=true' : ''}`),
  );

  const retire = useMutation((metricId: string) => api.post(`/metrics/${metricId}/retire`), {
    invalidates: ['metrics', 'audit'],
    onSuccess: () => {
      toast.push('指标版本已停用');
      setRetiring(null);
    },
  });

  const rows = metrics.data ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>指标定义</h1>
        <span className="small muted">
          检测任务声明要测哪些指标、结果回传按指标版本入账、统计按指标版本分组。
          允许范围是录入校验，不是质量判定——值在范围内也仍然要复核。
        </span>
      </div>

      <Panel
        title={`指标版本（${rows.length}）`}
        aside={
          <div className="filters">
            <label className="small">
              <input
                type="checkbox"
                checked={onlyActive}
                onChange={(event) => setOnlyActive(event.target.checked)}
              />
              只看在用
            </label>
            {can('metric.edit') ? (
              <button className="btn primary sm" onClick={() => setCreating(true)}>
                登记指标
              </button>
            ) : null}
          </div>
        }
        flush
      >
        <ListState
          loading={metrics.loading && !metrics.data}
          error={metrics.error}
          empty={!rows.length}
          emptyText="还没有指标定义。检测任务必须引用已登记的指标版本，请先登记。"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>指标</th>
                <th>版本</th>
                <th>类型</th>
                <th>录入校验</th>
                <th>适用样本</th>
                <th>被引用</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <b>{row.name}</b>
                    <div className="tiny muted mono">{row.code}</div>
                  </td>
                  <td className="mono small">
                    {row.version}
                    {row.method_version ? (
                      <div className="tiny muted">方法 {row.method_version}</div>
                    ) : null}
                  </td>
                  <td className="small">
                    {TYPE_LABEL[row.value_type] ?? row.value_type}
                    {row.unit ? <span className="mono"> · {row.unit}</span> : null}
                  </td>
                  <td className="small">{describeRules(row)}</td>
                  <td className="small">{row.sample_types.join('、') || <span className="muted">不限</span>}</td>
                  <td className="mono small">
                    {row.referenced_by}
                    {row.referenced_by ? <div className="tiny muted">已锁定口径</div> : null}
                  </td>
                  <td>
                    <Pill state={row.state} />
                  </td>
                  <td className="row-end">
                    {can('metric.edit') ? (
                      <>
                        <button
                          className="btn sm"
                          disabled={!row.editable}
                          title={row.editable ? '' : '已被结果引用，只能修订出新版本'}
                          onClick={() => setEditing(row)}
                        >
                          编辑
                        </button>
                        <button className="btn sm" onClick={() => setRevising(row)}>
                          修订
                        </button>
                        {row.state === 'active' ? (
                          <button className="btn sm danger" onClick={() => setRetiring(row)}>
                            停用
                          </button>
                        ) : null}
                      </>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      {creating ? <MetricForm mode="create" onClose={() => setCreating(false)} /> : null}
      {editing ? <MetricForm mode="edit" metric={editing} onClose={() => setEditing(null)} /> : null}
      {revising ? <MetricForm mode="revise" metric={revising} onClose={() => setRevising(null)} /> : null}
      {retiring ? (
        <ConfirmDialog
          title={`停用 · ${retiring.code} ${retiring.version}`}
          danger
          confirmLabel="停用"
          pending={retire.pending}
          error={retire.error?.message}
          onConfirm={() => retire.run(retiring.id)}
          onClose={() => setRetiring(null)}
        >
          停用后新的检测任务不能再引用这个版本，已采集的结果与统计不受影响——它们指向的是
          这一版的口径，记录必须留着才解释得了历史数据。
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function describeRules(row: MetricRow): string {
  if (row.value_type === 'enum') {
    return (row.rules.options ?? []).join('、') || '未定义可选值';
  }
  if (row.value_type !== 'number') return '—';
  const { min, max } = row.rules;
  if (min === undefined && max === undefined) return '不限';
  if (min !== undefined && max !== undefined) return `${min} ~ ${max}`;
  return min !== undefined ? `≥ ${min}` : `≤ ${max}`;
}

/* 一个表单三种用途：登记、改本版、修订出新版。差别只在提交到哪个接口、
   以及哪些字段能改——代码与版本号在「改本版」时不可动。 */
function MetricForm({
  mode,
  metric,
  onClose,
}: {
  mode: 'create' | 'edit' | 'revise';
  metric?: MetricRow;
  onClose: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState({
    code: metric?.code ?? '',
    name: metric?.name ?? '',
    version: mode === 'revise' ? nextVersion(metric?.version ?? 'v1') : metric?.version ?? 'v1',
    value_type: metric?.value_type ?? ('number' as MetricRow['value_type']),
    unit: metric?.unit ?? '',
    method_version: metric?.method_version ?? '',
    sample_types: (metric?.sample_types ?? []).join('、'),
    min: metric?.rules.min ?? '',
    max: metric?.rules.max ?? '',
    options: (metric?.rules.options ?? []).join('、'),
  });

  const rules = () => {
    if (form.value_type === 'enum') {
      return { options: splitList(form.options) };
    }
    if (form.value_type !== 'number') return {};
    const out: Record<string, number> = {};
    if (form.min !== '' && Number.isFinite(Number(form.min))) out.min = Number(form.min);
    if (form.max !== '' && Number.isFinite(Number(form.max))) out.max = Number(form.max);
    return out;
  };

  const body = () => ({
    code: form.code.trim(),
    name: form.name.trim(),
    version: form.version.trim(),
    value_type: form.value_type,
    unit: form.unit.trim(),
    method_version: form.method_version.trim(),
    sample_types: splitList(form.sample_types),
    rules: rules(),
  });

  const submit = useMutation(
    () => {
      if (mode === 'create') return api.post('/metrics', body());
      if (mode === 'revise') return api.post(`/metrics/${metric!.id}/revisions`, body());
      const { code: _code, version: _version, ...patch } = body();
      return api.patch(`/metrics/${metric!.id}`, patch);
    },
    {
      invalidates: ['metrics', 'audit'],
      onSuccess: () => {
        toast.push(mode === 'revise' ? '已修订出新版本' : mode === 'edit' ? '指标已更新' : '指标已登记');
        onClose();
      },
    },
  );

  const numberMissingUnit = form.value_type === 'number' && !form.unit.trim();
  const enumMissingOptions = form.value_type === 'enum' && !splitList(form.options).length;
  const title =
    mode === 'create' ? '登记指标' : mode === 'edit' ? `编辑 · ${metric!.code} ${metric!.version}` : `修订 · ${metric!.code}`;

  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={
              !form.code || !form.name || !form.version || numberMissingUnit ||
              enumMissingOptions || submit.pending
            }
            onClick={() => submit.run().catch(() => undefined)}
          >
            {mode === 'revise' ? '建立新版本' : '保存'}
          </button>
        </>
      }
    >
      {mode === 'revise' ? (
        <div className="note">
          修订出新版本，原版本保持不变：已按原版本采集的结果与已发布报告仍指向它，
          口径不会被回头改掉。新的检测任务请引用新版本。
        </div>
      ) : null}
      {mode === 'edit' && metric?.referenced_by ? (
        <div className="note bad">
          这个版本已被 {metric.referenced_by} 条结果引用，服务端会拒绝修改。请改用「修订」。
        </div>
      ) : null}

      <div className="grid cols-2">
        <Field label="指标代码" hint={mode === 'edit' ? '已登记的代码不可改' : '英文小写与下划线，如 areal_density'}>
          <input
            value={form.code}
            readOnly={mode === 'edit'}
            onChange={(event) => setForm({ ...form, code: event.target.value })}
          />
        </Field>
        <Field label="版本" hint={mode === 'edit' ? '改版本等于换口径，请用修订' : '如 v1、v2'}>
          <input
            value={form.version}
            readOnly={mode === 'edit'}
            onChange={(event) => setForm({ ...form, version: event.target.value })}
          />
        </Field>
      </div>
      <Field label="名称">
        <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
      </Field>
      <div className="grid cols-2">
        <Field label="值类型">
          <select
            value={form.value_type}
            onChange={(event) =>
              setForm({ ...form, value_type: event.target.value as MetricRow['value_type'] })
            }
          >
            {VALUE_TYPES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="标准单位"
          hint={form.value_type === 'number' ? '数值指标必填：没有单位的数字无法比较' : '非数值指标可留空'}
        >
          <input
            value={form.unit}
            onChange={(event) => setForm({ ...form, unit: event.target.value })}
          />
        </Field>
      </div>
      <Field label="检测方法版本" hint="留档用：同一指标在不同方法下的可比性由它说明">
        <input
          value={form.method_version}
          onChange={(event) => setForm({ ...form, method_version: event.target.value })}
        />
      </Field>
      <Field label="适用样本类型" hint="顿号或逗号分隔；留空表示不限">
        <input
          value={form.sample_types}
          onChange={(event) => setForm({ ...form, sample_types: event.target.value })}
        />
      </Field>

      {form.value_type === 'number' ? (
        <>
          <div className="grid cols-2">
            <Field label="允许下限" hint="留空表示不限">
              <input
                value={String(form.min)}
                onChange={(event) => setForm({ ...form, min: event.target.value })}
              />
            </Field>
            <Field label="允许上限" hint="留空表示不限">
              <input
                value={String(form.max)}
                onChange={(event) => setForm({ ...form, max: event.target.value })}
              />
            </Field>
          </div>
          <div className="note">
            这是**录入校验**：超出范围的回传会被整条拒绝。它不代表质量合格——范围内的值
            同样要经过数据复核才算有效。
          </div>
        </>
      ) : null}
      {form.value_type === 'enum' ? (
        <Field label="可选值" hint="顿号或逗号分隔，回传只接受这些值">
          <input
            value={form.options}
            onChange={(event) => setForm({ ...form, options: event.target.value })}
          />
        </Field>
      ) : null}
      {metric ? (
        <div className="small muted">登记于 {clock(metric.created_at)}</div>
      ) : null}
      {submit.error ? <div className="note bad">{submit.error.message}</div> : null}
    </Modal>
  );
}

function splitList(text: string): string[] {
  return text.split(/[、,，\s]+/).map((item) => item.trim()).filter(Boolean);
}

/* v1 → v2。非 vN 形式的版本号原样返回，让人自己填。 */
function nextVersion(current: string): string {
  const match = /^v(\d+)$/.exec(current.trim());
  return match ? `v${Number(match[1]) + 1}` : '';
}
