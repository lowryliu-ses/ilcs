/* 能力字典：流程步骤「做什么」的词表。

   能力的参数定义决定流程、设备方法能填哪些字段，恢复规则（能不能保持、能不能重试、用后要不要清洗）
   由每个引用它的步骤继承；工位能力极限、校准适用范围、人员资质、SOP 适用能力也都按它登记。
   它不属于某一台工位，所以单独成页；各工位能实现到什么范围在「工位与接入」里按工位改（能力极限只有那一个入口）。 */
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import type { CapabilityRow, ParamSpec, Recovery } from '../../shared/types';
import { withUnit } from '../../shared/units';
import { ConfirmDialog, Field, ListState, Modal, NumberInput, Panel, Pill, useToast } from '../../shared/ui';

export function CapabilitiesPage() {
  const { can } = useSession();
  const toast = useToast();
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const [registering, setRegistering] = useState(false);
  const [editing, setEditing] = useState<CapabilityRow | null>(null);
  const [deleting, setDeleting] = useState<CapabilityRow | null>(null);

  const retire = useMutation(
    (payload: { id: string; retired: boolean }) =>
      api.post(`/capabilities/${payload.id}/retire`, { retired: payload.retired }),
    {
      invalidates: ['capabilities', 'recipes', 'audit'],
      onSuccess: () => toast.push('能力状态已更新；已有流程与批次快照不受影响'),
    },
  );

  const remove = useMutation((capabilityId: string) => api.remove(`/capabilities/${capabilityId}`), {
    invalidates: ['capabilities', 'stations', 'audit'],
    onSuccess: () => {
      toast.push('能力已删除');
      setDeleting(null);
    },
  });

  const rows = capabilities.data ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>能力字典</h1>
          <div className="small muted">
            流程步骤、设备方法、工位极限、校准范围与人员资质共用的词表；参数定义与恢复规则改动要签名并重校验引用流程
          </div>
        </div>
        {can('station.edit') ? (
          <button className="btn primary" onClick={() => setRegistering(true)}>
            登记新能力
          </button>
        ) : null}
      </div>

      <Panel title={`能力与恢复规则（${rows.length}）`} flush>
        <ListState loading={capabilities.loading && !capabilities.data} error={capabilities.error} empty={!rows.length} emptyText="还没有登记能力" />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>能力</th>
                <th>参数</th>
                <th>保持</th>
                <th>重试</th>
                <th>恢复前核实</th>
                <th>实现工位</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((capability) => (
                <tr key={capability.id} className={capability.retired ? 'retired-row' : undefined}>
                  <td>
                    {capability.name}
                    {capability.retired ? <Pill state="retired" label="已停用" /> : null}
                    <div className="tiny muted mono">{capability.id}</div>
                  </td>
                  <td className="small">
                    {Object.entries(capability.params)
                      .map(([key, label]) => withUnit(label, capability.param_specs?.[key]?.unit ?? ''))
                      .join(' · ') || '无参数'}
                  </td>
                  <td className="small">
                    {capability.recovery.pausable ? `≤ ${capability.recovery.maxHoldMin} min` : '不可保持'}
                    {capability.recovery.hold ? <div className="tiny muted">{capability.recovery.hold}</div> : null}
                  </td>
                  <td className="small">
                    {capability.recovery.retryable ? '可重试' : '不可重试'}
                    {capability.recovery.cleanAfter ? <div className="tiny warn-text">用后需清洗确认</div> : null}
                    {capability.recovery.sideEffect ? (
                      <div className="tiny muted">{capability.recovery.sideEffect}</div>
                    ) : null}
                  </td>
                  <td className="small muted">{capability.recovery.verify?.join('、') || '无'}</td>
                  <td className="small mono">
                    {capability.stations.length ? (
                      <Link to="/stations">{capability.stations.join('、')}</Link>
                    ) : (
                      '无'
                    )}
                    {capability.recipes.length ? (
                      <div className="tiny muted">{capability.recipes.length} 个流程在用</div>
                    ) : null}
                  </td>
                  <td className="row-end">
                    {can('station.edit') ? (
                      <>
                        <button className="btn sm" onClick={() => setEditing(capability)}>
                          编辑
                        </button>
                        <button
                          className="btn sm"
                          title={capability.retired ? '恢复可选' : '新流程步骤不能再选它，已有流程不受影响'}
                          onClick={() =>
                            retire
                              .run({ id: capability.id, retired: !capability.retired })
                              .catch((error) => toast.push(error.message))
                          }
                        >
                          {capability.retired ? '启用' : '停用'}
                        </button>
                        <button
                          className="btn sm danger"
                          disabled={capability.delete_blockers.length > 0}
                          title={capability.delete_blockers.join('；') || undefined}
                          onClick={() => setDeleting(capability)}
                        >
                          删除
                        </button>
                      </>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <div className="panel-body small muted">
          各工位对每项能力能做到的参数范围（能力极限）在<Link to="/stations">工位与接入</Link>里按工位编辑；「怎么做」（设备端程序、缺省参数、应回报的数据）在<Link to="/methods">设备方法</Link>里按版本发布。
        </div>
      </Panel>

      {registering ? <CapabilityForm onClose={() => setRegistering(false)} /> : null}
      {editing ? <CapabilityEditForm capability={editing} onClose={() => setEditing(null)} /> : null}
      {deleting ? (
        <ConfirmDialog
          title={`删除能力 · ${deleting.name}`}
          danger
          confirmLabel="删除"
          pending={remove.pending}
          error={remove.error?.message}
          onClose={() => setDeleting(null)}
          onConfirm={() => remove.run(deleting.id).catch(() => undefined)}
        >
          <div className="note warn">
            <span className="mono">{deleting.id}</span> 没有工位实现、也没有流程引用，可以从字典里移除。
            有引用的能力请改用「停用」。
          </div>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

/* 登记新能力：参数定义 + 恢复规则。恢复规则写在能力上而不是流程上，
   因为「能不能保持、能不能重试」是设备物理属性，流程无权覆盖。
   哪些工位能做、参数范围多少不在这里勾：能力极限只在「工位与接入 → 编辑极限」里填，一条签名、一种检查。 */
function CapabilityForm({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  const [id, setId] = useState('cap.');
  const [name, setName] = useState('');
  const [params, setParams] = useState<ParamRow[]>([blankParam()]);
  const [recovery, setRecovery] = useState<Recovery>({
    pausable: true, maxHoldMin: 30, hold: '', retryable: false, sideEffect: '', verify: [],
  });
  const [verifyText, setVerifyText] = useState('');
  const [error, setError] = useState('');

  const create = useMutation((payload: Record<string, unknown>) => api.post('/capabilities', payload), {
    invalidates: ['capabilities', 'stations', 'recipes', 'audit'],
    onSuccess: () => {
      toast.push('能力已登记；到「工位与接入 → 编辑极限」给能做它的工位加上参数范围');
      onClose();
    },
  });

  const validParams = params.filter((row) => row.key.trim());
  const idOk = /^cap\.[a-z0-9_]+$/.test(id);
  const ready = idOk && name.trim().length > 0;

  const submit = async () => {
    if (!ready) {
      setError('标识需形如 cap.xxx（小写字母、数字、下划线），名称不能为空');
      return;
    }
    setError('');
    const signatureId = await sign('登记新能力', `${id} ${name}`, ['能力模型变更批准']);
    if (!signatureId) return;
    await create
      .run({
        id: id.trim(),
        name: name.trim(),
        params: paramLabels(validParams),
        param_specs: paramSpecs(validParams),
        recovery: {
          ...recovery,
          verify: verifyText.split(/[、,，\s]+/).map((item) => item.trim()).filter(Boolean),
        },
        signature_id: signatureId,
      })
      .catch((caught) => setError(caught.message));
  };

  return (
    <Modal
      title="登记新能力"
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={create.pending} onClick={submit}>
            签名并登记
          </button>
        </>
      }
    >
      <div className="note">
        能力是流程步骤的绑定对象。参数定义决定流程里能填哪些字段，恢复规则由能力继承到每一个引用它的步骤。
      </div>

      <div className="grid cols-2">
        <Field label="标识" hint="形如 cap.dose_solid，登记后不可更改">
          <input
            className={`mono${idOk ? '' : ' bad'}`}
            value={id}
            onChange={(event) => setId(event.target.value)}
          />
        </Field>
        <Field label="名称">
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：超声分散" />
        </Field>
      </div>

      <div>
        <div className="small muted" style={{ marginBottom: 6 }}>
          参数定义（键用于流程与指令，标签用于界面显示；单位单独登记，前馈换算与因子单位核对按它）
        </div>
        <ParamRowsEditor rows={params} onChange={setParams} />
      </div>

      <div className="grid cols-2">
        <label className="check">
          <input
            type="checkbox"
            checked={!!recovery.pausable}
            onChange={(event) => setRecovery((current) => ({ ...current, pausable: event.target.checked }))}
          />
          可保持（异常时能停在中间态）
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={!!recovery.retryable}
            onChange={(event) => setRecovery((current) => ({ ...current, retryable: event.target.checked }))}
          />
          可重试（重做本步不产生不可逆副作用）
        </label>
        <label className="check" title="做完后工位转为待清洗；在「现场监控」确认已清洗之前，别的批次的动作不投递">
          <input
            type="checkbox"
            checked={!!recovery.cleanAfter}
            onChange={(event) => setRecovery((current) => ({ ...current, cleanAfter: event.target.checked }))}
          />
          用后需清洗确认（确认前不给别的批次用）
        </label>
      </div>

      <div className="grid cols-2">
        <Field label="最长保持时长 min">
          <NumberInput
            value={recovery.maxHoldMin ?? ''}
            disabled={!recovery.pausable}
            ariaLabel="最长保持时长"
            onChange={(next) => setRecovery((current) => ({ ...current, maxHoldMin: next === '' ? 0 : next }))}
          />
        </Field>
        <Field label="保持状态描述">
          <input
            value={recovery.hold ?? ''}
            disabled={!recovery.pausable}
            placeholder="例如：换能器停振，冷却水继续循环"
            onChange={(event) => setRecovery((current) => ({ ...current, hold: event.target.value }))}
          />
        </Field>
      </div>

      <Field label="重试副作用说明" hint="不可重试时这句话会出现在恢复评估里，解释为什么只能终止">
        <input
          value={recovery.sideEffect ?? ''}
          onChange={(event) => setRecovery((current) => ({ ...current, sideEffect: event.target.value }))}
        />
      </Field>

      <Field label="恢复前核实项" hint="用顿号或逗号分隔">
        <input value={verifyText} onChange={(event) => setVerifyText(event.target.value)} placeholder="累计超声时间、浆料温度" />
      </Field>

      <div className="small muted">
        登记之后到<Link to="/stations">工位与接入</Link>，在能做它的工位上「编辑极限」加上这项能力、按实际标定填参数范围；
        没有工位登记它之前，引用它的流程步骤找不到可承接的工位。
      </div>

      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/* 改能力定义。增删参数会改变所有引用流程的校验结果，所以签名并当场报告影响面。 */
function CapabilityEditForm({ capability, onClose }: { capability: CapabilityRow; onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  const [name, setName] = useState(capability.name);
  const [params, setParams] = useState<ParamRow[]>(() =>
    Object.entries(capability.params ?? {}).map(([key, label]) => {
      const spec = capability.param_specs?.[key] ?? {};
      return { key, label, unit: spec.unit ?? '', type: spec.type ?? 'number', required: spec.required !== false };
    }),
  );
  const [recovery, setRecovery] = useState<Recovery>({ ...capability.recovery });
  const [verifyText, setVerifyText] = useState((capability.recovery.verify ?? []).join('、'));
  const [error, setError] = useState('');

  const save = useMutation(
    (payload: Record<string, unknown>) => api.patch<{ broken_recipes: string[] }>(`/capabilities/${capability.id}`, payload),
    {
      invalidates: ['capabilities', 'stations', 'recipes', 'audit'],
      onSuccess: (result) => {
        toast.push(
          result.broken_recipes.length
            ? `已保存；${result.broken_recipes.join('、')} 重校验不再通过`
            : '已保存并重新校验全部引用流程',
        );
        onClose();
      },
    },
  );

  const removed = Object.keys(capability.params ?? {}).filter((key) => !params.some((p) => p.key.trim() === key));

  const submit = async () => {
    const signatureId = await sign('修改能力定义', `${capability.id} ${name}`, ['能力模型变更批准']);
    if (!signatureId) return;
    await save
      .run({
        name,
        params: paramLabels(params.filter((p) => p.key.trim())),
        param_specs: paramSpecs(params.filter((p) => p.key.trim())),
        recovery: { ...recovery, verify: verifyText.split(/[、,，\s]+/).map((x) => x.trim()).filter(Boolean) },
        signature_id: signatureId,
      })
      .catch((caught) => setError(caught.message));
  };

  return (
    <Modal
      title={`编辑能力 · ${capability.id}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={save.pending} onClick={submit}>签名并保存</button>
        </>
      }
    >
      {capability.recipes.length ? (
        <div className="note warn">
          {capability.recipes.length} 个流程的步骤在用它（{capability.recipes.slice(0, 5).join('、')}）。
          改参数定义会立刻重算它们的校验结果。
        </div>
      ) : null}
      {removed.length ? (
        <div className="note bad">
          将移除参数 <span className="mono">{removed.join('、')}</span>：引用它的流程步骤会变成「参数不属于该能力」，
          各工位极限里的对应条目也会一并清掉。
        </div>
      ) : null}

      <Field label="名称">
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </Field>

      <div>
        <div className="small muted" style={{ marginBottom: 6 }}>参数定义</div>
        <ParamRowsEditor rows={params} onChange={setParams} />
      </div>

      <div className="grid cols-2">
        <label className="check">
          <input type="checkbox" checked={!!recovery.pausable}
            onChange={(e) => setRecovery((c) => ({ ...c, pausable: e.target.checked }))} />
          可保持
        </label>
        <label className="check">
          <input type="checkbox" checked={!!recovery.retryable}
            onChange={(e) => setRecovery((c) => ({ ...c, retryable: e.target.checked }))} />
          可重试
        </label>
        <label className="check" title="做完后工位转为待清洗；在「现场监控」确认已清洗之前别的批次的动作不投递">
          <input type="checkbox" checked={!!recovery.cleanAfter}
            onChange={(e) => setRecovery((c) => ({ ...c, cleanAfter: e.target.checked }))} />
          用后需清洗确认
        </label>
      </div>
      <div className="grid cols-2">
        <Field label="最长保持时长 min">
          <NumberInput value={recovery.maxHoldMin ?? ''} disabled={!recovery.pausable} ariaLabel="最长保持时长"
            onChange={(v) => setRecovery((c) => ({ ...c, maxHoldMin: v === '' ? 0 : v }))} />
        </Field>
        <Field label="保持状态描述">
          <input value={recovery.hold ?? ''} disabled={!recovery.pausable}
            onChange={(e) => setRecovery((c) => ({ ...c, hold: e.target.value }))} />
        </Field>
      </div>
      <Field label="重试副作用说明">
        <input value={recovery.sideEffect ?? ''} onChange={(e) => setRecovery((c) => ({ ...c, sideEffect: e.target.value }))} />
      </Field>
      <Field label="恢复前核实项" hint="用顿号或逗号分隔">
        <input value={verifyText} onChange={(e) => setVerifyText(e.target.value)} />
      </Field>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

type ParamRow = { key: string; label: string; unit: string; type: 'number' | 'integer'; required: boolean };

function blankParam(): ParamRow {
  return { key: '', label: '', unit: '', type: 'number', required: true };
}

function paramLabels(rows: ParamRow[]): Record<string, string> {
  return Object.fromEntries(rows.map((row) => [row.key.trim(), row.label.trim() || row.key.trim()]));
}

/** 只提交与缺省（数值、单位未登记、必填）不同的规格，服务端也按同一规则收成规范写法。 */
function paramSpecs(rows: ParamRow[]): Record<string, ParamSpec> {
  return Object.fromEntries(
    rows.map((row) => [row.key.trim(), { type: row.type, unit: row.unit.trim(), required: row.required }]),
  );
}

/** 参数表：键、显示名称、单位、类型、是否必填。单位用于前馈换算与因子单位核对，不从显示名称里猜。 */
function ParamRowsEditor({ rows, onChange }: { rows: ParamRow[]; onChange: (rows: ParamRow[]) => void }) {
  const patch = (index: number, changes: Partial<ParamRow>) =>
    onChange(rows.map((row, order) => (order === index ? { ...row, ...changes } : row)));
  return (
    <>
      <table>
        <thead>
          <tr>
            <th>键</th>
            <th>标签</th>
            <th>单位</th>
            <th>类型</th>
            <th>必填</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <td>
                <input
                  className="mono"
                  value={row.key}
                  aria-label={`参数 ${index + 1} 键`}
                  placeholder="power"
                  onChange={(event) => patch(index, { key: event.target.value })}
                />
              </td>
              <td>
                <input
                  value={row.label}
                  aria-label={`参数 ${index + 1} 标签`}
                  placeholder="超声功率"
                  onChange={(event) => patch(index, { label: event.target.value })}
                />
              </td>
              <td>
                <input
                  className="mono"
                  value={row.unit}
                  aria-label={`参数 ${index + 1} 单位`}
                  placeholder="W"
                  style={{ width: 72 }}
                  onChange={(event) => patch(index, { unit: event.target.value })}
                />
              </td>
              <td>
                <select
                  value={row.type}
                  aria-label={`参数 ${index + 1} 类型`}
                  onChange={(event) => patch(index, { type: event.target.value as ParamRow['type'] })}
                >
                  <option value="number">数值</option>
                  <option value="integer">整数</option>
                </select>
              </td>
              <td>
                <input
                  type="checkbox"
                  checked={row.required}
                  aria-label={`参数 ${index + 1} 必填`}
                  title="不勾选：流程步骤可以不写这个参数，设备按自己的缺省值执行"
                  onChange={(event) => patch(index, { required: event.target.checked })}
                />
              </td>
              <td className="row-end">
                <button
                  className="btn sm"
                  aria-label={`删除参数 ${index + 1}`}
                  onClick={() => onChange(rows.filter((_, order) => order !== index))}
                >
                  删
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="btn sm" style={{ marginTop: 8 }} onClick={() => onChange([...rows, blankParam()])}>
        添加参数
      </button>
    </>
  );
}
