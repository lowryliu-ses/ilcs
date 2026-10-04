/* 设备接入模板：一类设备怎么接——驱动 + 映射配置（点表、命令、状态码、参数对应）+ 连接参数示例 + 支持标志 + 验收缺省。

   工位 = 模板的某一版 + 自己的连接参数（地址、证书、设备编号）：同型号的几台设备共用一份，不再各抄一份 JSON。
   起草 → 另一个人签名发布（起草人不能发布本人起草的），发布后内容冻结，要改就新建修订；新修订发布时旧版退役，
   但不自动推给工位——下面列出还在用旧修订的工位，逐台切换、重新验收。
   设备模块交付的 profile.json 就是这里的导出文件：导入一律成草稿，文件摘要对不上（导出后被改过）直接拒绝。

   它是「工位与接入」的一个页签（/stations/templates）：只在工位的设备连接里用得到，不单独占一个菜单。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { ForceDeleteButton } from '../../shared/forceDelete';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import type { DeviceTemplateRow, DriverField, DriverInfo } from '../../shared/types';
import { Field, ListState, Modal, Panel, Pill, useToast } from '../../shared/ui';
import { ConfigEditor, useFormContext } from './ConfigForm';

const STATE_PILL: Record<string, string> = { draft: 'scheduled', released: 'running', retired: 'done' };
const SUPPORTS = [['hold', '保持'], ['abort', '终止'], ['query', '按指令查询'], ['dedup', '设备端去重']] as const;

export function DeviceTemplatesTab() {
  const toast = useToast();
  const { can } = useSession();
  const { sign } = useSignature();
  const [state, setState] = useState('');
  const [opened, setOpened] = useState<string | 'new' | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const key = `device-templates:${state}`;
  const templates = useQuery<DeviceTemplateRow[]>(key, () =>
    api.get<DeviceTemplateRow[]>(`/device-templates${state ? `?state=${state}` : ''}`),
  );
  const invalidates = ['device-templates', 'stations'];

  const importFile = useMutation(
    async (file: File) => {
      const document = JSON.parse(await file.text());
      return api.post<DeviceTemplateRow>('/device-templates/import', { filename: file.name, document });
    },
    {
      invalidates,
      onSuccess: (row) => {
        toast.push(`已导入为 ${row.code} r${row.revision} 草稿：核对后由另一个人签名发布`);
        setOpened(row.id);
      },
    },
  );

  const act = useMutation(
    async ({ row, action }: { row: DeviceTemplateRow; action: 'release' | 'revise' | 'retire' | 'delete' }) => {
      if (action === 'delete') return api.remove<DeviceTemplateRow>(`/device-templates/${row.id}`);
      if (action === 'revise') return api.post<DeviceTemplateRow>(`/device-templates/${row.id}/revise`);
      if (action === 'retire') return api.post<DeviceTemplateRow>(`/device-templates/${row.id}/retire`, { row_version: row.row_version });
      const signatureId = await sign('发布设备接入模板', row.id, ['发布设备接入模板'], row.row_version);
      if (!signatureId) return null;
      return api.post<DeviceTemplateRow>(`/device-templates/${row.id}/release`, { row_version: row.row_version, signature_id: signatureId });
    },
    {
      invalidates,
      onSuccess: (result) => {
        if (result) toast.push('已更新');
      },
    },
  );
  const run = (row: DeviceTemplateRow, action: 'release' | 'revise' | 'retire' | 'delete') =>
    act.run({ row, action }).catch((error) => toast.push(error.message));

  return (
    <>
      <Panel
        title="接入模板"
        aside={
          <div className="filters">
            <select value={state} onChange={(event) => setState(event.target.value)}>
              <option value="">全部状态</option>
              <option value="draft">草稿</option>
              <option value="released">已发布</option>
              <option value="retired">已退役</option>
            </select>
            {can('station.edit') ? (
              <>
                <input
                  ref={fileInput}
                  type="file"
                  accept=".json,application/json"
                  style={{ display: 'none' }}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    event.target.value = '';
                    if (file) importFile.run(file).catch((error) => toast.push(error.message));
                  }}
                />
                <button className="btn sm" disabled={importFile.pending} onClick={() => fileInput.current?.click()}>导入模板文件</button>
                <button className="btn primary sm" onClick={() => setOpened('new')}>起草模板</button>
              </>
            ) : null}
          </div>
        }
        flush
      >
        <ListState loading={templates.loading && !templates.data} error={templates.error} empty={!templates.data?.length}
          emptyText="还没有设备接入模板：导入设备模块的 profile.json，或从已接好的工位抄一份映射起草" />
        {templates.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>编号</th>
                <th>名称</th>
                <th>驱动</th>
                <th>适用型号</th>
                <th>在用工位</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {templates.data.map((row) => (
                <tr key={row.id}>
                  <td className="mono small">{row.code} r{row.revision}</td>
                  <td>
                    <b>{row.name}</b>
                    <div className="tiny muted">
                      {row.vendor || '—'} · {row.version || '—'}
                      {row.source?.kind === 'import' ? ` · 导入自 ${row.source.file || '文件'}` : ''}
                    </div>
                  </td>
                  <td className="small">{row.driver_label}<div className="tiny muted mono">{row.driver}</div></td>
                  <td className="small">{row.model || '不限'}</td>
                  <td className="small">
                    {row.usage.stations}
                    {row.state === 'released' && row.usage.outdated ? (
                      <div className="tiny warn-text">另有 {row.usage.outdated} 个工位还在用旧修订</div>
                    ) : null}
                  </td>
                  <td>
                    <Pill state={STATE_PILL[row.state] ?? 'neutral'} label={row.state_label} />
                    {row.released_at ? <div className="tiny muted">{clock(row.released_at)} · {row.released_by_name}</div> : null}
                  </td>
                  <td className="row-end">
                    <button className="btn sm" onClick={() => setOpened(row.id)}>{row.state === 'draft' && can('station.edit') ? '编辑' : '查看'}</button>
                    {row.state === 'draft' && can('template.release') ? (
                      <button className="btn sm primary" disabled={act.pending} onClick={() => run(row, 'release')}>发布</button>
                    ) : null}
                    {row.state !== 'draft' && can('station.edit') ? (
                      <button className="btn sm" disabled={act.pending} onClick={() => run(row, 'revise')}>修订</button>
                    ) : null}
                    {row.state === 'released' && can('template.release') ? (
                      <button className="btn sm danger" disabled={act.pending} onClick={() => run(row, 'retire')}>退役</button>
                    ) : null}
                    <button className="btn sm"
                      onClick={() => api.download(`/device-templates/${row.id}/export`, `${row.code}-r${row.revision}.json`).catch((error) => toast.push(error.message))}>
                      导出
                    </button>
                    {row.state === 'draft' && can('station.edit') ? (
                      <button className="btn sm" disabled={act.pending} onClick={() => run(row, 'delete')}>删除</button>
                    ) : (
                      <ForceDeleteButton kind="template" id={row.id} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      <div className="note">
        发布之后内容冻结：改点表、改命令都要新建修订、重新发布。新修订不会自动推给工位，套用旧修订的工位照常运行；
        在工位的<Link to="/stations/connections">设备连接</Link>里切到新修订要签名，保存后执行器自动跑接入验收。
      </div>

      {opened ? (
        <TemplateDialog
          templateId={opened === 'new' ? null : opened}
          editable={can('station.edit')}
          invalidates={invalidates}
          onClose={() => setOpened(null)}
        />
      ) : null}
    </>
  );
}

type Obj = Record<string, unknown>;

/** 驱动示例配置拆成两半：连接参数（每台设备自己填）与映射（模板里写死）；加一项能力时照 capability_examples 起步 */
function splitExample(info: DriverInfo): { mapping: Obj; connection: Obj; example: Obj } {
  const connection = Object.fromEntries(info.connection_keys.filter((name) => name in info.template).map((name) => [name, info.template[name]]));
  const mapping = Object.fromEntries(Object.entries(info.template).filter(([name]) => !info.connection_keys.includes(name)));
  const capabilities = { ...(info.capability_examples ?? {}), ...((info.template.capabilities as Obj | undefined) ?? {}) };
  return { mapping, connection, example: { ...mapping, capabilities } };
}

function TemplateDialog({
  templateId, editable, invalidates, onClose,
}: {
  templateId: string | null;
  editable: boolean;
  invalidates: string[];
  onClose: () => void;
}) {
  const toast = useToast();
  const detail = useQuery<DeviceTemplateRow>(templateId ? `device-templates:detail:${templateId}` : null, () =>
    api.get<DeviceTemplateRow>(`/device-templates/${templateId}`),
  );
  const drivers = useQuery<DriverInfo[]>('drivers:', () => api.get<DriverInfo[]>('/drivers'));
  const row = detail.data;
  const draft = !templateId || row?.state === 'draft';
  const readOnly = !editable || !draft;
  const [form, setForm] = useState<Record<string, string> | null>(null);
  const [config, setConfig] = useState<Obj>({});
  const [connection, setConnection] = useState<Obj>({});
  const [acceptance, setAcceptance] = useState<Obj>({});
  // 哪一块的 JSON 写错了：改好之前不能保存
  const [invalid, setInvalid] = useState<Record<string, string>>({});
  const [supports, setSupports] = useState<Record<string, boolean>>({ hold: true, abort: true, query: true, dedup: true });
  const [error, setError] = useState('');
  const context = useFormContext();

  useEffect(() => {
    if (form || (templateId && !row)) return;
    setForm({
      code: row?.code ?? '', name: row?.name ?? '', driver: row?.driver ?? 'http_json_v1', model: row?.model ?? '',
      vendor: row?.vendor ?? '', protocol: row?.protocol ?? '', version: row?.version ?? '', note: row?.note ?? '',
    });
    setConfig(row?.config ?? {});
    setConnection(row?.connection ?? {});
    setAcceptance(row?.acceptance ?? { capability: '', params: {} });
    if (row?.supports) setSupports({ hold: true, abort: true, query: true, dedup: true, ...row.supports });
  }, [form, row, templateId]);
  const info = drivers.data?.find((item) => item.key === form?.driver);
  const split = useMemo(() => (info ? splitExample(info) : null), [info]);
  const mappingFields = useMemo(() => (info?.fields ?? []).filter((field) => !field.connection && field.name !== 'acceptance'), [info]);
  const connectionFields = useMemo(() => (info?.fields ?? []).filter((field) => field.connection), [info]);
  const acceptanceFields = useMemo<DriverField[]>(() => info?.fields.find((field) => field.name === 'acceptance')?.fields ?? [], [info]);
  const markInvalid = (part: string) => (message: string) => setInvalid((current) => ({ ...current, [part]: message }));

  const save = useMutation(
    async () => {
      if (!form) return null;
      const broken = Object.values(invalid).find(Boolean);
      if (broken) throw new Error(broken);
      const payload = {
        name: form.name, driver: form.driver, model: form.model, vendor: form.vendor, protocol: form.protocol,
        version: form.version, note: form.note, supports, config, connection, acceptance,
      };
      return row
        ? api.patch<DeviceTemplateRow>(`/device-templates/${row.id}`, { ...payload, row_version: row.row_version })
        : api.post<DeviceTemplateRow>('/device-templates', { ...payload, code: form.code.trim() });
    },
    {
      invalidates,
      onSuccess: (saved) => {
        if (!saved) return;
        toast.push(saved.check?.ok ? '已保存草稿' : `已保存；发布前还要处理：${saved.check?.problems[0] ?? ''}`);
        onClose();
      },
    },
  );

  const startFromDriver = () => {
    if (!info || !form || !split) return;
    setForm({ ...form, protocol: form.protocol || info.protocol });
    setConfig(split.mapping);
    setConnection(split.connection);
    setSupports({ ...info.supports });
  };

  if (templateId && !row) {
    return <Modal title="设备接入模板" onClose={onClose}><div className="muted">{detail.error?.message ?? '正在读取…'}</div></Modal>;
  }
  if (!form) return null;
  const set = (name: string, value: string) => setForm((current) => (current ? { ...current, [name]: value } : current));

  return (
    <Modal
      title={row ? `${row.code} r${row.revision} · ${row.state_label}` : '起草设备接入模板'}
      wide
      onClose={onClose}
      footer={readOnly ? undefined : (
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={save.pending} onClick={() => save.run().catch((caught) => setError(caught.message))}>
            保存草稿
          </button>
        </>
      )}
    >
      {row?.check && (row.check.problems.length || row.check.warnings.length) ? (
        <div className={`note${row.check.ok ? '' : ' bad'}`}>
          {row.check.ok ? '可以发布；提醒：' : '发布前要处理：'}
          <ul className="issue-list">
            {row.check.problems.map((item) => <li key={item}>{item}</li>)}
            {row.check.warnings.map((item) => <li key={item} className="muted">{item}</li>)}
          </ul>
        </div>
      ) : null}
      <div className="grid cols-3">
        <Field label="模板编号" hint="同一类设备的各修订共用编号">
          <input className="mono" value={form.code} readOnly={!!row || readOnly} onChange={(event) => set('code', event.target.value)} placeholder="TPL-VAC-OVEN" />
        </Field>
        <Field label="名称">
          <input value={form.name} readOnly={readOnly} onChange={(event) => set('name', event.target.value)} />
        </Field>
        <Field label="驱动" hint={info?.summary}>
          <select value={form.driver} disabled={readOnly} onChange={(event) => set('driver', event.target.value)}>
            {(drivers.data ?? []).map((item) => <option key={item.key} value={item.key}>{item.label} · {item.key}</option>)}
          </select>
        </Field>
        <Field label="适用型号" hint="与「仪器设备」登记的型号一致时，登记同型号工位会排在前面">
          <input value={form.model} readOnly={readOnly} onChange={(event) => set('model', event.target.value)} />
        </Field>
        <Field label="厂家">
          <input value={form.vendor} readOnly={readOnly} onChange={(event) => set('vendor', event.target.value)} />
        </Field>
        <Field label="协议 / 版本">
          <div className="row">
            <input value={form.protocol} readOnly={readOnly} placeholder={info?.protocol} onChange={(event) => set('protocol', event.target.value)} />
            <input value={form.version} readOnly={readOnly} placeholder="命令手册 2.3" onChange={(event) => set('version', event.target.value)} />
          </div>
        </Field>
      </div>
      {!readOnly && info ? (
        <div className="row">
          <button type="button" className="btn sm" onClick={startFromDriver}>从 {info.label} 的示例配置开始</button>
          <span className="small muted">连接参数（{info.connection_keys.join('、') || '无'}）放进「连接参数示例」，其余放进映射配置</span>
        </div>
      ) : null}
      {info ? (
        <>
          <ConfigEditor
            label="映射配置"
            hint="不含每台设备的连接参数；点表、命令模板、状态映射、故障码按设备手册写"
            fields={mappingFields}
            known={info.fields}
            value={config}
            onChange={setConfig}
            onInvalid={markInvalid('config')}
            readOnly={readOnly}
            context={context}
            example={split?.example}
            elsewhere={(name) => (name === 'acceptance' ? '写在下面的「验收缺省」里' : '连接参数写在下面的「连接参数示例」里，套用时由工位填')}
          />
          <div className="grid cols-2">
            <ConfigEditor label="连接参数示例" hint="套用时由工位填写；示例里的 <占位> 必须换掉" fields={connectionFields} showAll
              value={connection} onChange={setConnection} onInvalid={markInvalid('connection')} readOnly={readOnly} context={context}
              example={split?.connection} rows={6} />
            <ConfigEditor label="验收缺省" hint="动作级验收用的能力与参数" fields={acceptanceFields} showAll
              value={acceptance} onChange={setAcceptance} onInvalid={markInvalid('acceptance')} readOnly={readOnly} context={context} rows={6} />
          </div>
        </>
      ) : (
        <div className="muted">{drivers.error?.message ?? '正在读取驱动目录…'}</div>
      )}
      <Field label="支持的控制动作" hint="只有设备真实支持时才勾选；不支持的动作在执行界面禁用并说明原因">
        <div className="row">
          {SUPPORTS.map(([name, label]) => (
            <label className="check" key={name}>
              <input type="checkbox" checked={supports[name] ?? true} disabled={readOnly}
                onChange={(event) => setSupports((current) => ({ ...current, [name]: event.target.checked }))} />
              {label}
            </label>
          ))}
        </div>
      </Field>
      <Field label="说明">
        <textarea rows={2} value={form.note} readOnly={readOnly} onChange={(event) => set('note', event.target.value)} />
      </Field>
      {row ? (
        <div className="small muted">
          摘要 <span className="mono">{row.digest.slice(0, 23)}</span> · 起草 {row.created_by_name || '—'}
          {row.released_by_name ? ` · 发布 ${row.released_by_name}` : ''}
          {row.source?.kind === 'import' ? ` · 导入自 ${row.source.file}` : row.source?.kind === 'revise' ? ` · 修订自 ${row.source.from}` : ''}
        </div>
      ) : null}
      {row?.stations?.length ? (
        <div className="subsection">
          <b>套用这个编号的工位</b>
          <table className="compact">
            <thead><tr><th>工位</th><th>套用修订</th><th>配置</th><th>接入验收</th></tr></thead>
            <tbody>
              {row.stations.map((item) => (
                <tr key={item.station_id}>
                  <td className="small"><span className="mono">{item.station_id}</span> {item.station_name}</td>
                  <td className={`small${item.outdated ? ' warn-text' : ''}`}>
                    r{item.revision}{item.outdated ? `（已发布 r${item.latest_revision}，到「设备连接」里切换）` : ''}
                  </td>
                  <td className="small mono">v{item.config_version}</td>
                  <td className="small">{item.acceptance_required ? `待接入验收（${item.acceptance_required === 'physical' ? '动作级' : '只读级'}）` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {row?.revisions && row.revisions.length > 1 ? (
        <div className="small muted">修订：{row.revisions.map((item) => `r${item.revision} ${item.state_label}`).join(' · ')}</div>
      ) : null}
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}
