/* 设备连接（适配器配置）：怎么连这台设备。两种写法——

   - 按模板：选一份已发布的设备接入模板（一类设备怎么接），只填这台设备自己的连接参数（地址、证书、设备编号）；
     同型号的几台设备共用一份模板，模板出新修订时这里提示，由人逐台切换；
   - 手工配置：选驱动，从驱动登记的示例配置开始改。表单按驱动自己声明的配置项出（后端驱动目录，含点表、能力映射、
     命令列表这类嵌套结构），不写死在页面里；完整 JSON 随时可以切过去直接改。

   保存要签名：配置版本递增、强制离线重新握手，并按驱动检查配置——配错了当场拒绝，不用等到测试连接；
   设备上还有可能在动作的指令时，换驱动、换连接目标、改状态映射会被拒绝。
   保存后执行器自动跑一次只读级接入验收，通过了才重新接指令（见下方「接入验收」）。

   还没接设备的工位（登记时没填协议）用同一个表单接入：两种写法照旧，保存改为登记（POST），
   测试连接、读取设备自报信息、接入验收要等接入之后才有。 */
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, api } from '../../shared/api';
import { time } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSignature } from '../../shared/signature';
import type {
  AdapterCatalog, AdapterRow, AdapterTestResult, ConfigCheck, DriverField, DriverInfo, StationRow, TemplateOption,
} from '../../shared/types';
import { Field, Modal, useToast } from '../../shared/ui';
import { AcceptancePanel } from './AcceptancePanel';
import { PointsPanel } from './PointsPanel';
import { ConfigEditor, ConfigForm, useFormContext } from './ConfigForm';

type Issues = { message: string; problems: string[]; warnings: string[]; blocked: string[] };

/** 保存被拒时的说明：配置问题逐条列出，「设备还在动作」列出是哪些指令 */
function issuesOf(caught: unknown): Issues {
  if (!(caught instanceof ApiError)) return { message: String(caught), problems: [], warnings: [], blocked: [] };
  const detail = (caught.payload as { detail?: { problems?: string[]; warnings?: string[] } })?.detail ?? {};
  return {
    message: caught.message, problems: detail.problems ?? [], warnings: detail.warnings ?? [],
    blocked: caught.blocked.map((row) => row.label),
  };
}

function IssueNote({ issues }: { issues: Issues | null }) {
  if (!issues) return null;
  return (
    <div className="note bad">
      {issues.problems.length ? '保存被拒：配置有问题' : issues.message}
      {issues.problems.length ? <ul className="issue-list">{issues.problems.map((item) => <li key={item}>{item}</li>)}</ul> : null}
      {issues.blocked.length ? <ul className="issue-list">{issues.blocked.map((item) => <li key={item}>{item}</li>)}</ul> : null}
      {issues.warnings.length ? (
        <div className="small">提醒：<ul className="issue-list">{issues.warnings.map((item) => <li key={item}>{item}</li>)}</ul></div>
      ) : null}
    </div>
  );
}

function CheckNote({ check }: { check: ConfigCheck | null }) {
  if (!check) return null;
  return (
    <div className={`note${check.ok ? '' : ' bad'}`}>
      {check.ok ? '配置检查通过（按驱动字段与构造驱动实例，没有连设备）' : '配置检查没通过：'}
      {check.problems.length ? <ul className="issue-list">{check.problems.map((item) => <li key={item}>{item}</li>)}</ul> : null}
      {check.warnings.length ? (
        <div className="small">提醒：<ul className="issue-list">{check.warnings.map((item) => <li key={item}>{item}</li>)}</ul></div>
      ) : null}
    </div>
  );
}

type Obj = Record<string, unknown>;

/** 连接参数里还留着示例占位（`<设备>`）的第一处：套用模板时必须换成这台设备的值 */
function placeholderIn(value: unknown): string {
  if (typeof value === 'string') return /<[^>]+>/.test(value) ? value : '';
  if (Array.isArray(value)) return value.map(placeholderIn).find(Boolean) ?? '';
  if (value && typeof value === 'object') return Object.values(value).map(placeholderIn).find(Boolean) ?? '';
  return '';
}

/** 驱动示例配置 + 每项能力的起步写法：表单里加一项能力时照它起步 */
function exampleOf(info?: DriverInfo): Obj | undefined {
  if (!info) return undefined;
  return { ...info.template, capabilities: { ...(info.capability_examples ?? {}), ...((info.template.capabilities as Obj | undefined) ?? {}) } };
}

/** 还没接设备的工位：表单从这份空白开始，缺省是内置模拟，选了驱动的示例配置就切到真实设备 */
function blankAdapter(): AdapterRow {
  return {
    kind: 'simulation', driver: 'simulation', protocol: '', version: '', config: {}, credential_ref: '',
    credential_configured: false, config_version: 0, enabled: true, row_version: 0, updated_at: '', status: 'offline',
    connected: false, accepts_commands: false, site_interlock: false, dedup_count: 0, last_heartbeat: '',
    heartbeat_age_sec: 0, current_command_id: '', note: '', unsupported_note: '',
    capabilities: { hold: true, abort: true, query: true, dedup: true }, template: null, template_connection: {},
  };
}

export function AdapterEditor({ station, onClose }: { station: StationRow; onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  // 列表里没有适配器的工位是「还没接设备」：不读详情（会 404），从空白开始，保存走登记
  const creating = !station.adapter;
  const detail = useQuery<AdapterRow>(
    creating ? null : `adapter-detail-${station.id}`, () => api.get<AdapterRow>(`/stations/${station.id}/adapter`),
  );
  const drivers = useQuery<DriverInfo[]>(`drivers:${station.id}`, () => api.get<DriverInfo[]>(`/drivers?station_id=${station.id}`));
  const options = useQuery<TemplateOption[]>(
    `adapter-templates:${station.id}`, () => api.get<TemplateOption[]>(`/stations/${station.id}/adapter/templates`),
  );
  const [mode, setMode] = useState<'template' | 'manual'>(creating ? 'template' : 'manual');
  const [draft, setDraft] = useState<AdapterRow | null>(null);
  const [config, setConfig] = useState<Obj>({});
  // 完整 JSON 写错时的说明：改好之前不能检查、不能保存
  const [configInvalid, setConfigInvalid] = useState('');
  const context = useFormContext(station.limits);
  const [issues, setIssues] = useState<Issues | null>(null);
  const [check, setCheck] = useState<ConfigCheck | null>(null);
  const [testResult, setTestResult] = useState<AdapterTestResult | null>(null);

  useEffect(() => {
    if (creating) {
      setDraft((current) => current ?? blankAdapter());
      return;
    }
    if (!detail.data) return;
    setDraft(detail.data);
    setConfig(detail.data.config ?? {});
    setMode(detail.data.template ? 'template' : 'manual');
  }, [creating, detail.data]);
  // 接入时没有可套用的已发布模板：直接落到手工配置
  useEffect(() => {
    if (creating && options.data && !options.data.length) setMode('manual');
  }, [creating, options.data]);

  const invalidates = ['stations', `adapter-detail-${station.id}`, `adapter-templates:${station.id}`, 'gate', 'audit', 'device-templates'];
  const save = useMutation(
    (payload: Record<string, unknown>) => (creating
      ? api.post<AdapterRow>(`/stations/${station.id}/adapter`, payload)
      : api.patch<AdapterRow>(`/stations/${station.id}/adapter`, payload)),
    {
      invalidates,
      onSuccess: (saved) => {
        if (creating) {
          toast.push(saved.acceptance?.required
            ? `已接入 ${station.id}；执行器正在做接入验收，通过后才接指令`
            : `已接入 ${station.id}；等执行器握手后上线`);
          return;
        }
        toast.push(saved.acceptance?.required
          ? `已保存配置 v${saved.config_version}；执行器正在做接入验收，通过后恢复接指令`
          : '连接配置已保存；连接状态已清除，等执行器重新握手');
      },
    },
  );
  const describe = useMutation(
    () => api.post<AdapterCatalog & { warning: string }>(`/stations/${station.id}/adapter/describe`),
    {
      invalidates: ['stations', `adapter-detail-${station.id}`, 'audit'],
      onSuccess: (result) =>
        toast.push(result.warning || `已读取 ${result.methods.length} 个设备端程序（${result.described_from === 'device' ? '设备自报' : '按登记配置'}）`),
    },
  );
  const test = useMutation(() => api.post<AdapterTestResult>(`/stations/${station.id}/adapter/test`), {
    onSuccess: (result) => {
      setTestResult(result);
      toast.push(result.contract.kind === 'simulation' ? '模拟器健康检查通过（不代表真实设备）' : '真实设备健康检查通过');
    },
  });

  const driverInfo = drivers.data?.find((row) => row.key === draft?.driver);
  const update = <K extends keyof AdapterRow>(key: K, value: AdapterRow[K]) =>
    setDraft((current) => (current ? { ...current, [key]: value } : current));

  const applyDriverTemplate = (info: DriverInfo) => {
    setDraft((current) => current ? {
      ...current, kind: 'real', driver: info.key, protocol: info.protocol, version: '1.0',
      credential_ref: info.credential.replace('<工位>', station.id), capabilities: { ...info.supports },
    } : current);
    setConfig(info.template);
    setCheck(null);
  };

  const parsedConfig = (): Obj | null => {
    if (configInvalid) {
      setIssues({ message: configInvalid, problems: [], warnings: [], blocked: [] });
      return null;
    }
    return config;
  };

  const runCheck = async () => {
    if (!draft) return;
    const config = parsedConfig();
    if (!config) return;
    setIssues(null);
    try {
      setCheck(await api.post<ConfigCheck>(`/stations/${station.id}/adapter/check`, {
        driver: draft.driver, protocol: draft.protocol, config, credential_ref: draft.credential_ref.trim(),
      }));
    } catch (caught) {
      setIssues(issuesOf(caught));
    }
  };

  const signAndSave = async (payload: Record<string, unknown>) => {
    if (!draft) return;
    const signatureId = creating
      ? await sign('登记设备适配器', station.id, ['设备集成配置变更批准'])
      : await sign('修改设备适配器', station.id, ['设备集成配置变更批准'], draft.row_version);
    if (!signatureId) return;
    setIssues(null);
    await save.run({ ...payload, ...(creating ? {} : { row_version: draft.row_version }), signature_id: signatureId })
      .then(() => onClose())
      .catch((caught) => setIssues(issuesOf(caught)));
  };

  const submitManual = async () => {
    if (!draft) return;
    const config = parsedConfig();
    if (!config) return;
    if (draft.kind === 'real' && (!draft.driver.trim() || draft.driver === 'simulation')) {
      setIssues({ message: '真实设备必须选一个已登记的驱动', problems: [], warnings: [], blocked: [] });
      return;
    }
    await signAndSave({
      protocol: draft.protocol,
      driver: draft.kind === 'simulation' ? 'simulation' : draft.driver.trim(),
      version: draft.version,
      kind: draft.kind,
      config,
      credential_ref: draft.credential_ref.trim(),
      enabled: draft.enabled,
      supports_hold: draft.capabilities.hold,
      supports_abort: draft.capabilities.abort,
      supports_query: draft.capabilities.query,
      supports_dedup: draft.capabilities.dedup,
      note: draft.note,
    });
  };

  if (!creating && detail.loading && !draft) {
    return <Modal title={`设备连接 · ${station.id}`} onClose={onClose}><div className="muted">正在读取受控配置…</div></Modal>;
  }
  if ((!creating && detail.error) || !draft) {
    return <Modal title={`设备连接 · ${station.id}`} onClose={onClose}><div className="note bad">{detail.error?.message ?? '适配器不存在'}</div></Modal>;
  }

  return (
    <Modal
      title={`${creating ? '接入设备' : '设备连接'} · ${station.id}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          {creating ? null : (
            <>
              <button className="btn" disabled={test.pending} onClick={() => test.run().catch((caught) => setIssues(issuesOf(caught)))}>
                {test.pending ? '测试中…' : '测试已保存配置'}
              </button>
              <button className="btn" disabled={describe.pending} onClick={() => describe.run().catch((caught) => setIssues(issuesOf(caught)))}>
                {describe.pending ? '读取中…' : '读取设备自报信息'}
              </button>
            </>
          )}
          {mode === 'manual' ? (
            <>
              <button className="btn" onClick={runCheck}>检查配置</button>
              <button className="btn primary" disabled={save.pending} onClick={submitManual}>{creating ? '签名并接入' : '签名并保存'}</button>
            </>
          ) : null}
        </>
      }
    >
      <div className="subsection-head">
        <div className="seg">
          <button type="button" className={mode === 'template' ? 'on' : ''} onClick={() => setMode('template')}>按设备接入模板</button>
          <button type="button" className={mode === 'manual' ? 'on' : ''} onClick={() => setMode('manual')}>手工配置</button>
        </div>
        {creating ? (
          <span className="small muted">尚未接入：保存后等执行器握手才算在线，真实设备还要先过接入验收</span>
        ) : (
          <span className="small muted">
            当前配置 v{draft.config_version} · 行版本 v{draft.row_version} · 凭据{draft.credential_configured ? '已配置' : '未配置'}
            {draft.template ? ` · 模板 ${draft.template.code ?? ''} r${draft.template.revision ?? ''}` : ''}
          </span>
        )}
      </div>

      {mode === 'template' ? (
        <TemplateForm
          station={station}
          adapter={draft}
          creating={creating}
          drivers={drivers.data ?? []}
          options={options.data ?? []}
          pending={save.pending}
          onSave={signAndSave}
          onIssues={setIssues}
        />
      ) : (
        <>
          <div className="note warn">
            {creating ? '接入时按驱动检查配置' : '保存会递增配置版本、强制离线，并按驱动检查配置'}；密钥原文不得写进 JSON，只能填写密钥管理器引用。
            <div className="row">
              从驱动的示例配置开始：
              {(drivers.data ?? []).map((info) => (
                <button key={info.key} type="button" className="btn small" title={info.summary} onClick={() => applyDriverTemplate(info)}>
                  {info.label}
                </button>
              ))}
            </div>
          </div>
          <div className="grid cols-3">
            <Field label="模式">
              <select value={draft.kind} onChange={(event) => update('kind', event.target.value as AdapterRow['kind'])}>
                <option value="simulation">模拟器</option>
                <option value="real">真实设备</option>
              </select>
            </Field>
            <Field label="驱动" hint={driverInfo ? `${driverInfo.summary}；指令号与去重：${driverInfo.ledger}` : '选一个已登记的驱动'}>
              <select
                value={draft.kind === 'simulation' ? 'simulation' : draft.driver}
                disabled={draft.kind === 'simulation'}
                onChange={(event) => update('driver', event.target.value)}
              >
                <option value="simulation" disabled>（内置模拟）</option>
                {(drivers.data ?? []).map((info) => <option key={info.key} value={info.key}>{info.label} · {info.key}</option>)}
              </select>
            </Field>
            <Field label="状态">
              <label className="check">
                <input type="checkbox" checked={draft.enabled} onChange={(event) => update('enabled', event.target.checked)} />
                启用适配器
              </label>
            </Field>
          </div>
          <div className="grid cols-2">
            <Field label="协议名称">
              <input value={draft.protocol} placeholder="SiLA 2（驱动宿主）/ HTTPS JSON" onChange={(event) => update('protocol', event.target.value)} />
            </Field>
            <Field label="协议/驱动版本">
              <input value={draft.version} onChange={(event) => update('version', event.target.value)} />
            </Field>
          </div>
          <ConfigEditor
            label="连接配置"
            hint={driverInfo && draft.kind === 'real' ? '按驱动的配置项填；保存前可以先「检查配置」' : '内置模拟没有登记配置项，按 JSON 写'}
            fields={driverInfo && draft.kind === 'real' ? driverInfo.fields : []}
            jsonOnly={!(driverInfo && draft.kind === 'real')}
            value={config}
            onChange={(next) => { setConfig(next); setCheck(null); }}
            onInvalid={setConfigInvalid}
            context={context}
            example={exampleOf(driverInfo)}
          />
          <CheckNote check={check} />
          {driverInfo && draft.kind === 'real' ? <DriverFields info={driverInfo} /> : null}
          <Field label="凭据引用" hint="只接受 vault://、env://、file://；不要填写密码、token 或私钥原文">
            <input className="mono" value={draft.credential_ref} placeholder="vault://ilcs/devices/ST-01" onChange={(event) => update('credential_ref', event.target.value)} />
          </Field>
          <Field label="支持的控制动作" hint="只勾设备真实支持的；不支持的动作在执行界面禁用并说明原因">
            <div className="row">
              {([['hold', '保持'], ['abort', '终止'], ['query', '按指令查询'], ['dedup', '设备端去重']] as const).map(([key, label]) => (
                <label className="check" key={key}>
                  <input
                    type="checkbox"
                    checked={draft.capabilities[key]}
                    onChange={(event) => update('capabilities', { ...draft.capabilities, [key]: event.target.checked })}
                  />
                  {label}
                </label>
              ))}
            </div>
          </Field>
          <Field label="说明">
            <textarea rows={2} value={draft.note} onChange={(event) => update('note', event.target.value)} />
          </Field>
          {draft.template ? (
            <div className="note warn">这台设备现在按模板 {draft.template.code} r{draft.template.revision} 管理：手工改了完整配置并保存后，就不再按模板管理。</div>
          ) : null}
        </>
      )}

      <IssueNote issues={issues} />
      {creating ? null : <CatalogNote catalog={detail.data?.catalog} />}
      {creating || !detail.data?.driver_info?.config_digest ? null : <DriverNote station={station} adapter={detail.data} />}
      {testResult ? <div className="note">健康检查结果：<span className="mono">{JSON.stringify(testResult.health)}</span></div> : null}
      {test.error ? <div className="note bad">{test.error.message}</div> : null}
      {detail.data ? <AcceptancePanel station={station} adapter={detail.data} /> : null}
      {detail.data && detail.data.kind === 'real' && detail.data.points ? <PointsPanel station={station} adapter={detail.data} /> : null}
    </Modal>
  );
}

function DriverFields({ info }: { info: DriverInfo }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="small">
      <button type="button" className="btn sm" onClick={() => setOpen(!open)}>{open ? '收起' : '展开'}驱动配置项（{info.fields.length}）</button>
      {open ? (
        <table className="compact">
          <thead><tr><th>配置项</th><th>类型</th><th>说明</th></tr></thead>
          <tbody>
            {info.fields.map((field) => (
              <tr key={field.name}>
                <td className="mono">{field.name}{field.required ? ' *' : ''}</td>
                <td>{field.type_label}{field.connection ? ' · 连接参数' : ''}</td>
                <td>{field.label}{field.hint ? `：${field.hint}` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function TemplateForm({
  station, adapter, creating, drivers, options, pending, onSave, onIssues,
}: {
  station: StationRow;
  adapter: AdapterRow;
  creating: boolean;
  drivers: DriverInfo[];
  options: TemplateOption[];
  pending: boolean;
  onSave: (payload: Record<string, unknown>) => Promise<void>;
  onIssues: (issues: Issues | null) => void;
}) {
  const current = adapter.template;
  // 已套用的模板有新修订时缺省选新修订（切换要签名保存），否则选当前那一版
  const initial = current?.outdated ? current.latest_id ?? current.id : current?.id ?? options[0]?.id ?? '';
  const [selected, setSelected] = useState(initial);
  useEffect(() => { if (!selected && options.length) setSelected(initial); }, [options, initial, selected]);
  const option = options.find((row) => row.id === selected);
  const info = drivers.find((row) => row.key === option?.driver);
  const fields = useMemo(
    () => (option?.connection_keys ?? []).map((name) =>
      info?.fields.find((field) => field.name === name)
      ?? { name, label: name, type: 'string' as const, type_label: '文本', required: false, connection: true, hint: '' }),
    [option, info],
  );
  const [connection, setConnection] = useState<Obj>({});
  const [credential, setCredential] = useState(adapter.credential_ref);
  const context = useFormContext(station.limits);
  useEffect(() => {
    if (!option) return;
    const stored = adapter.template_connection ?? {};
    setConnection(Object.fromEntries(fields
      .map((field) => [field.name, field.name in stored ? stored[field.name] : option.connection?.[field.name]] as const)
      .filter(([, value]) => value !== undefined)));
  }, [option, fields, adapter.template_connection]);

  if (!options.length && !current) {
    return (
      <div className="note">
        还没有可套用的设备接入模板。到<Link to="/stations/templates">接入模板</Link>导入设备模块的 profile.json、发布之后再来；
        或者切到「手工配置」。
      </div>
    );
  }

  const submit = async () => {
    const values: Obj = {};
    for (const field of fields) {
      const value = connection[field.name];
      if (value === undefined || value === '' || value === null) {
        if (field.required) {
          onIssues({ message: `请填写「${field.label}」`, problems: [], warnings: [], blocked: [] });
          return;
        }
        continue;
      }
      const placeholder = placeholderIn(value);
      if (placeholder) {
        onIssues({ message: `「${field.label}」还是示例占位（${placeholder}），请换成这台设备的值`, problems: [], warnings: [], blocked: [] });
        return;
      }
      values[field.name] = value;
    }
    await onSave({ template_id: selected, template_connection: values, credential_ref: credential.trim() });
  };

  return (
    <div className="subsection" style={{ borderTop: 0, marginTop: 0, paddingTop: 0 }}>
      {current ? (
        <div className={`note${current.outdated ? ' warn' : ''}`}>
          按模板 <b>{current.code} r{current.revision}</b>（{current.name}）管理
          {current.state_label ? ` · ${current.state_label}` : ''}
          {current.outdated ? `：已发布新修订 r${current.latest_revision}，不会自动切换——核对后选新修订、签名保存，保存后要重新验收。` : '。'}
          <div className="row">
            <button type="button" className="btn sm" disabled={pending} onClick={() => onSave({ template_id: '' })}>
              不再按模板管理（配置原样保留）
            </button>
          </div>
        </div>
      ) : null}
      <Field label="设备接入模板" hint="只列已发布的模板；适用型号与本工位一致的排在前面">
        <select value={selected} onChange={(event) => setSelected(event.target.value)}>
          {current && !options.some((row) => row.id === current.id) ? (
            <option value={current.id}>{current.code} r{current.revision}（{current.state_label ?? '当前'}）</option>
          ) : null}
          {options.map((row) => (
            <option key={row.id} value={row.id}>
              {row.code} r{row.revision} · {row.name}{row.model ? ` · ${row.model}` : ''}{row.matches_model ? '（型号一致）' : ''}
            </option>
          ))}
        </select>
      </Field>
      {option && info ? <div className="small muted">驱动 {info.label}：{info.summary}；指令号与去重：{info.ledger}</div> : null}
      {fields.length ? (
        <ConfigForm fields={fields} showAll value={connection} onChange={setConnection} context={context} example={option?.connection} />
      ) : (
        <div className="small muted">这份模板没有要按工位填的连接参数</div>
      )}
      <Field label="凭据引用" hint={info?.credential ? `如 ${info.credential.replace('<工位>', station.id)}；不写密码原文` : '只接受 vault://、env://、file://'}>
        <input className="mono" value={credential} onChange={(event) => setCredential(event.target.value)} />
      </Field>
      <div className="row-end">
        <button className="btn primary" disabled={pending || !selected} onClick={submit}>
          {creating ? '套用模板并签名接入' : current && current.id === selected ? '签名并保存连接参数' : '套用模板并签名保存'}
        </button>
      </div>
    </div>
  );
}

function digest(value?: string) {
  return value ? value.replace('sha256:', '').slice(0, 12) : '—';
}

/** 驱动在 ILCS 之外的设备服务：报的驱动与配置摘要，和批准的那份对不对得上；变了要签名批准，再接入验收 */
function DriverNote({ station, adapter }: { station: StationRow; adapter: AdapterRow }) {
  const toast = useToast();
  const { sign } = useSignature();
  const [reason, setReason] = useState('');
  const [approving, setApproving] = useState(false);
  const approve = useMutation(
    (payload: Record<string, unknown>) => api.post(`/stations/${station.id}/adapter/driver-approval`, payload),
    {
      invalidates: [`adapter-detail-${station.id}`, `stations:acceptance:${station.id}`, 'stations', 'audit'],
      onSuccess: () => toast.push('已批准这次驱动变更：执行器马上跑只读级接入验收，通过后放行'),
    },
  );
  const reported = adapter.driver_info ?? {};
  const approved = adapter.approved_driver ?? {};
  const submit = async () => {
    if (reason.trim().length < 4) {
      toast.push('写明核对了什么：驱动项目里的哪次改动、改了什么');
      return;
    }
    const signatureId = await sign('批准驱动配置变更', station.id, ['批准驱动配置变更'], adapter.config_version);
    if (!signatureId) return;
    await approve.run({ reason: reason.trim(), signature_id: signatureId }).catch((error) => toast.push(error.message));
    setApproving(false);
    setReason('');
  };
  return (
    <div className={`note${adapter.driver_changed ? ' bad' : ''}`}>
      <b>设备服务的驱动</b>：{reported.plugin || '—'} {reported.plugin_version || ''} · 配置 {reported.config_version || '—'}（{digest(reported.config_digest)}）
      {' · '}
      {adapter.driver_changed
        ? `和批准的不一致（批准的是 ${approved.plugin || '—'} ${digest(approved.config_digest)}）：驱动项目里改过，`
          + (adapter.driver_awaiting_approval ? '核对后签名批准这次变更，再通过接入验收放行' : '已签名批准，等接入验收出结论')
        : approved.config_digest
          ? '已批准并通过接入验收'
          : '还没有通过接入验收'}
      {reported.reported_at ? <div className="small muted">最近一次探测：{time(reported.reported_at)}</div> : null}
      {adapter.driver_awaiting_approval ? (
        approving ? (
          <div className="driver-approval">
            <textarea
              rows={2}
              value={reason}
              placeholder="核对了什么：驱动项目里的哪次改动（提交号）、改了哪些点表或映射"
              onChange={(event) => setReason(event.target.value)}
            />
            <div className="row-end">
              <button className="btn sm" onClick={() => setApproving(false)}>取消</button>
              <button className="btn sm primary" disabled={approve.pending} onClick={submit}>签名批准</button>
            </div>
          </div>
        ) : (
          <div className="row-end">
            <button className="btn sm" onClick={() => setApproving(true)}>批准这次驱动变更…</button>
          </div>
        )
      ) : null}
    </div>
  );
}

function CatalogNote({ catalog }: { catalog?: AdapterCatalog }) {
  if (!catalog?.described_at) {
    return <div className="small muted">还没读取过设备自报信息（厂商、固件、程序清单）；流程引用设备方法时，这台设备按「未报目录」处理，不据程序排除。</div>;
  }
  return (
    <div className="note">
      <b>设备自报信息</b>（{catalog.described_from === 'device' ? '设备自报' : catalog.described_from === 'config' ? '按登记配置' : '无目录'} ·{' '}
      {time(catalog.described_at)}）：厂商 {catalog.vendor || '—'} · 型号 {catalog.reported_model || '—'} · 固件 {catalog.firmware || '—'}
      <div className="small">
        程序：{catalog.methods.map((row) => (row.program === '*' ? '任意程序' : `${row.program}${row.name !== row.program ? `（${row.name}）` : ''}`)).join('、') || '无'}
      </div>
      <div className="small muted">指令：{catalog.commands.join(' / ') || '—'}</div>
    </div>
  );
}
