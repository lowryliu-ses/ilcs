/* 设备方法目录：「流程管做什么，方法管怎么做」里的后一半。

   一条方法 = 能力 + 适用仪器型号 + 设备端程序 + 参数缺省值与允许范围 + 数据输出规则，按版本管理。
   起草 → 另一位有发布权限的人发布；发布新版本时同编号的旧发布版退役，引用旧版的流程要改引用并重新评审。
   流程的设备步骤引用已发布的方法，建批次时方法内容冻结进快照，指令带着设备端程序下发。 */
import { useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { CapabilityRow, DeviceMethodRow, MethodOutputRule, MethodParamRule } from '../../shared/types';
import { Field, ListState, Modal, Panel, Pill, useToast } from '../../shared/ui';

const STATE_PILL: Record<string, string> = { draft: 'scheduled', released: 'running', retired: 'done' };
type Action = 'release' | 'revise' | 'retire' | 'delete';
const DONE: Record<Action, string> = {
  release: '已发布；同编号旧版本已退役', revise: '已建修订草稿', retire: '已退役', delete: '已删除草稿',
};

export function MethodsPage() {
  const toast = useToast();
  const { can } = useSession();
  const [state, setState] = useState('');
  const [editing, setEditing] = useState<DeviceMethodRow | 'new' | null>(null);
  const key = `device-methods:${state}`;
  const methods = useQuery<DeviceMethodRow[]>(key, () =>
    api.get<DeviceMethodRow[]>(`/device-methods${state ? `?state=${state}` : ''}`),
  );
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const invalidates = ['device-methods'];
  const act = useMutation(
    async ({ row, action }: { row: DeviceMethodRow; action: Action }) => {
      if (action === 'delete') await api.remove(`/device-methods/${row.id}`);
      else await api.post(`/device-methods/${row.id}/${action}`, action === 'revise' ? {} : { row_version: row.row_version });
      return action;
    },
    { invalidates, onSuccess: (action) => toast.push(DONE[action]) },
  );
  const run = (row: DeviceMethodRow, action: Action) => act.run({ row, action }).catch((error) => toast.push(error.message));

  return (
    <div className="page">
      <div className="page-head">
        <h1>设备方法</h1>
        <div className="row">
          <select value={state} onChange={(event) => setState(event.target.value)}>
            <option value="">全部状态</option>
            <option value="draft">草稿</option>
            <option value="released">已发布</option>
            <option value="retired">已退役</option>
          </select>
          {can('method.edit') ? (
            <button className="btn primary" onClick={() => setEditing('new')}>
              起草方法
            </button>
          ) : null}
        </div>
      </div>
      <div className="note">
        流程的设备步骤写「做什么」（能力）；设备方法写「怎么做」：适用哪些型号、调用设备上的哪个程序、参数缺省值与允许范围、
        这一步应该回报哪些数据。流程引用方法后，只有型号适用、且驱动自报支持该程序的工位才会承接这一步。
      </div>

      <Panel title="方法目录" flush>
        <ListState loading={methods.loading && !methods.data} error={methods.error} empty={!methods.data?.length} emptyText="还没有设备方法" />
        {methods.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>编号</th>
                <th>名称</th>
                <th>能力</th>
                <th>设备端程序</th>
                <th>适用型号</th>
                <th>引用</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {methods.data.map((row) => (
                <tr key={row.id}>
                  <td className="mono small">
                    {row.code} v{row.version}
                  </td>
                  <td>
                    <b>{row.name}</b>
                    {row.issues.length ? <div className="tiny bad-text">{row.issues[0]}</div> : null}
                  </td>
                  <td className="small">{row.capability_name}</td>
                  <td className="small mono">{row.program || '—'}</td>
                  <td className="small">{row.instrument_models.join('、') || '不限'}</td>
                  <td className="small">
                    {row.used_by.length ? row.used_by.map((recipe) => `${recipe.id} v${recipe.version}`).join('、') : '—'}
                  </td>
                  <td>
                    <Pill state={STATE_PILL[row.state] ?? 'neutral'} label={row.state_label} />
                    {row.released_at ? <div className="tiny muted">{clock(row.released_at)}</div> : null}
                  </td>
                  <td className="row-end">
                    <button className="btn sm" onClick={() => setEditing(row)}>
                      {row.state === 'draft' && can('method.edit') ? '编辑' : '查看'}
                    </button>
                    {row.state === 'draft' && can('method.release') ? (
                      <button className="btn sm primary" disabled={act.pending || !!row.issues.length} onClick={() => run(row, 'release')}>
                        发布
                      </button>
                    ) : null}
                    {row.state !== 'draft' && can('method.edit') ? (
                      <button className="btn sm" disabled={act.pending} onClick={() => run(row, 'revise')}>
                        修订
                      </button>
                    ) : null}
                    {row.state === 'released' && can('method.release') ? (
                      <button className="btn sm danger" disabled={act.pending} onClick={() => run(row, 'retire')}>
                        退役
                      </button>
                    ) : null}
                    {row.state === 'draft' && can('method.edit') && !row.used_by.length ? (
                      <button className="btn sm" disabled={act.pending} onClick={() => run(row, 'delete')}>
                        删除
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      {editing ? (
        <MethodDialog
          method={editing === 'new' ? null : editing}
          capabilities={(capabilities.data ?? []).filter((row) => !row.retired)}
          readOnly={editing !== 'new' && (editing.state !== 'draft' || !can('method.edit'))}
          invalidates={invalidates}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </div>
  );
}

type ParamDraft = Record<string, { on: boolean; default: string; min: string; max: string; unit: string }>;

function toNumber(value: string): number | null {
  if (value.trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function MethodDialog({
  method,
  capabilities,
  readOnly,
  invalidates,
  onClose,
}: {
  method: DeviceMethodRow | null;
  capabilities: CapabilityRow[];
  readOnly: boolean;
  invalidates: string[];
  onClose: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(method?.name ?? '');
  const [capabilityId, setCapabilityId] = useState(method?.capability_id ?? capabilities[0]?.id ?? '');
  const [program, setProgram] = useState(method?.program ?? '');
  const [models, setModels] = useState((method?.instrument_models ?? []).join(', '));
  const [dur, setDur] = useState(String(method?.dur_min ?? 0));
  const [note, setNote] = useState(method?.note ?? '');
  const [params, setParams] = useState<ParamDraft>(() =>
    Object.fromEntries(
      Object.entries(method?.params ?? {}).map(([key, rule]) => [
        key,
        { on: true, default: String(rule.default ?? ''), min: String(rule.min ?? ''), max: String(rule.max ?? ''), unit: rule.unit ?? '' },
      ]),
    ),
  );
  const [outputs, setOutputs] = useState<MethodOutputRule[]>(method?.outputs ?? []);
  const capability = capabilities.find((row) => row.id === capabilityId);

  const payload = () => ({
    name,
    capability_id: capabilityId,
    program: program.trim(),
    instrument_models: models.split(/[,，、\s]+/).map((value) => value.trim()).filter(Boolean),
    dur_min: Number(dur) || 0,
    note,
    params: Object.fromEntries(
      Object.entries(params)
        .filter(([key, rule]) => rule.on && key in (capability?.params ?? {}))
        .map(([key, rule]): [string, MethodParamRule] => [
          key,
          { default: toNumber(rule.default), min: toNumber(rule.min), max: toNumber(rule.max), unit: rule.unit },
        ]),
    ),
    outputs: outputs.filter((row) => row.key.trim()),
  });
  const save = useMutation(
    () =>
      method
        ? api.patch<DeviceMethodRow>(`/device-methods/${method.id}`, { ...payload(), row_version: method.row_version })
        : api.post<DeviceMethodRow>('/device-methods', payload()),
    {
      invalidates,
      onSuccess: (row) => {
        toast.push(row.issues.length ? `已保存；发布前还要处理：${row.issues[0]}` : '已保存');
        onClose();
      },
    },
  );
  const setParam = (key: string, field: keyof ParamDraft[string], value: string | boolean) =>
    setParams((current) => ({
      ...current,
      [key]: { ...(current[key] ?? { on: false, default: '', min: '', max: '', unit: '' }), [field]: value },
    }));

  return (
    <Modal
      title={method ? `${method.code} v${method.version} · ${method.state_label}` : '起草设备方法'}
      wide
      onClose={onClose}
      footer={
        readOnly ? undefined : (
          <>
            <button className="btn" onClick={onClose}>
              取消
            </button>
            <button className="btn primary" disabled={save.pending || !name.trim() || !capabilityId} onClick={() => save.run().catch(() => undefined)}>
              保存草稿
            </button>
          </>
        )
      }
    >
      <div className="grid cols-2">
        <Field label="方法名称">
          <input value={name} readOnly={readOnly} onChange={(event) => setName(event.target.value)} placeholder="如：120℃ 真空干燥" />
        </Field>
        <Field label="能力" hint="流程步骤引用这条方法时，能力必须一致">
          <select value={capabilityId} disabled={readOnly} onChange={(event) => setCapabilityId(event.target.value)}>
            {capabilities.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="设备端程序" hint="驱动按它选设备上的程序 / 方法文件；设备自报的方法目录里没有它的工位不会承接">
          <input value={program} readOnly={readOnly} className="mono" onChange={(event) => setProgram(event.target.value)} placeholder="如：VD-120" />
        </Field>
        <Field label="适用仪器型号" hint="逗号分隔；留空表示实现了该能力的型号都可以">
          <input value={models} readOnly={readOnly} onChange={(event) => setModels(event.target.value)} placeholder="如：VAC-WEIGH-12" />
        </Field>
        <Field label="缺省时长（min）" hint="流程步骤没写时长时取这个值">
          <input type="number" min={0} value={dur} readOnly={readOnly} onChange={(event) => setDur(event.target.value)} />
        </Field>
        <Field label="说明">
          <input value={note} readOnly={readOnly} onChange={(event) => setNote(event.target.value)} />
        </Field>
      </div>

      <h4>参数（缺省值与允许范围）</h4>
      {Object.keys(capability?.params ?? {}).length ? (
        <table>
          <thead>
            <tr>
              <th>纳入</th>
              <th>参数</th>
              <th>缺省</th>
              <th>下限</th>
              <th>上限</th>
              <th>单位</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(capability?.params ?? {}).map(([key, label]) => {
              const rule = params[key] ?? { on: false, default: '', min: '', max: '', unit: '' };
              return (
                <tr key={key}>
                  <td>
                    <input type="checkbox" checked={rule.on} disabled={readOnly} onChange={(event) => setParam(key, 'on', event.target.checked)} />
                  </td>
                  <td className="small">
                    {label} <span className="tiny muted mono">{key}</span>
                  </td>
                  {(['default', 'min', 'max'] as const).map((field) => (
                    <td key={field}>
                      <input
                        type="number"
                        value={rule[field]}
                        disabled={readOnly || !rule.on}
                        style={{ width: 90 }}
                        onChange={(event) => setParam(key, field, event.target.value)}
                      />
                    </td>
                  ))}
                  <td>
                    <input value={rule.unit} disabled={readOnly || !rule.on} style={{ width: 70 }} onChange={(event) => setParam(key, 'unit', event.target.value)} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <div className="small muted">该能力没有参数</div>
      )}

      <h4>数据输出规则</h4>
      <div className="small muted">设备这一步应该回报的值与合理范围。越界的值照常入库并打标，交数据审核处理。</div>
      <table>
        <thead>
          <tr>
            <th>指标键</th>
            <th>名称</th>
            <th>单位</th>
            <th>下限</th>
            <th>上限</th>
            <th>必报</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {outputs.map((row, index) => {
            const update = (change: Partial<MethodOutputRule>) =>
              setOutputs((current) => current.map((item, at) => (at === index ? { ...item, ...change } : item)));
            return (
              <tr key={index}>
                <td>
                  <input className="mono" value={row.key} readOnly={readOnly} style={{ width: 120 }} onChange={(event) => update({ key: event.target.value })} />
                </td>
                <td>
                  <input value={row.label ?? ''} readOnly={readOnly} onChange={(event) => update({ label: event.target.value })} />
                </td>
                <td>
                  <input value={row.unit ?? ''} readOnly={readOnly} style={{ width: 70 }} onChange={(event) => update({ unit: event.target.value })} />
                </td>
                <td>
                  <input type="number" value={row.lo ?? ''} readOnly={readOnly} style={{ width: 90 }} onChange={(event) => update({ lo: toNumber(event.target.value) })} />
                </td>
                <td>
                  <input type="number" value={row.hi ?? ''} readOnly={readOnly} style={{ width: 90 }} onChange={(event) => update({ hi: toNumber(event.target.value) })} />
                </td>
                <td>
                  <input type="checkbox" checked={Boolean(row.required)} disabled={readOnly} onChange={(event) => update({ required: event.target.checked })} />
                </td>
                <td>
                  {readOnly ? null : (
                    <button className="btn sm" onClick={() => setOutputs((current) => current.filter((_, at) => at !== index))}>
                      删除
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {readOnly ? null : (
        <button className="btn sm" onClick={() => setOutputs((current) => [...current, { key: '', label: '', unit: '' }])}>
          添加输出
        </button>
      )}

      {method?.issues.length ? (
        <div className="note bad">
          发布前要处理：
          <ul>
            {method.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {method?.versions?.length ? (
        <div className="small muted">版本：{method.versions.map((row) => `v${row.version} ${row.state_label}`).join(' · ')}</div>
      ) : null}
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}
