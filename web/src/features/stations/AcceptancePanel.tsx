/* 设备接入验收：同一份检查清单——只读级查设备身份与方法目录、健康检查、契约声明、查询不存在的指令号；
   动作级再加正常完成、同一指令号重复提交、重建驱动后按指令号查回、保持、终止；模拟设备还能跑故障项目（丢回执、忙、联锁、失联）。
   由执行器执行（它是唯一驱动设备的进程），报告入库、只追加，带驱动、固件、配置与模板版本。

   配置变更后工位欠一份验收：欠着就按「待接入验收」挡住下发，执行器自动跑一次只读级；
   第一次接真实设备、换驱动时还要动作级（签名 + 现场批准人，DEC-02）。
   检查清单证明不了的设备（不支持状态查询、要现场摆位的动作）可以现场核对后签名放行，放行记录同样存档。 */
import { useEffect, useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { isOptionWindow } from '../../shared/params';
import { useMutation, useQuery } from '../../shared/query';
import { useSignature } from '../../shared/signature';
import type { AcceptanceDefaults, AcceptanceListing, AcceptanceRun, AdapterRow, LimitWindow, StationRow } from '../../shared/types';
import { Field, Modal, NumberInput, Pill, useToast } from '../../shared/ui';

const RUN_PILL: Record<string, string> = {
  queued: 'scheduled', running: 'running', done: 'done', error: 'fault', cancelled: 'retired',
};
const CHECK_PILL: Record<string, string> = { pass: 'running', fail: 'fault', skip: 'retired' };

/** 每个参数取一个一定落在极限里的值：数值取中点，选项型取第一个允许的选项（与服务端 default_template 一致） */
function midpoints(window: Record<string, LimitWindow> | undefined): Record<string, number | string> {
  return Object.fromEntries(
    Object.entries(window ?? {}).map(([name, span]) =>
      isOptionWindow(span) ? [name, span[0]] : [name, Number((((span[0] as number) + (span[1] as number)) / 2).toFixed(6))],
    ),
  );
}

const SOURCE_LABEL: Record<string, string> = { template: '设备接入模板的验收缺省', config: '连接配置的验收缺省', limits: '工位极限中点' };

/* 某项能力的初始参数：缺省（模板 / 配置）里给了就用它，数值参数进表单，其余（起止位置等）进 JSON；否则取极限中点 */
function initialParams(capability: string, window: Record<string, LimitWindow>, defaults?: AcceptanceDefaults) {
  const given = defaults && defaults.capability === capability ? defaults.params : {};
  const numeric: Record<string, number | string> = { ...midpoints(window) };
  const extra: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(given)) {
    if (name in window && (typeof value === 'number' || typeof value === 'string')) numeric[name] = value;
    else if (!(name in window)) extra[name] = value;
  }
  return { numeric, extra: Object.keys(extra).length ? JSON.stringify(extra, null, 2) : '' };
}

export function AcceptancePanel({ station, adapter }: { station: StationRow; adapter: AdapterRow }) {
  const toast = useToast();
  const { sign } = useSignature();
  const key = `stations:acceptance:${station.id}`;
  const [active, setActive] = useState(false);
  const listing = useQuery<AcceptanceListing>(
    key, () => api.get<AcceptanceListing>(`/stations/${station.id}/adapter/acceptance`), active ? 2000 : 0,
  );
  const busy = (listing.data?.runs ?? []).some((run) => run.state === 'queued' || run.state === 'running');
  // 有排队或执行中的验收时两秒刷新一次（推送断开时也看得到进度）；出了结论就停
  useEffect(() => setActive(busy), [busy]);
  const [physical, setPhysical] = useState(false);
  const [waiving, setWaiving] = useState(false);
  const [viewing, setViewing] = useState<string | null>(null);

  const request = useMutation(
    (payload: Record<string, unknown>) => api.post<AcceptanceRun>(`/stations/${station.id}/adapter/acceptance`, payload),
    {
      invalidates: [key, 'stations'],
      onSuccess: (run) =>
        toast.push(run.waiting_for
          ? `已排队：设备上还有 ${run.waiting_for} 条指令在动作，等它们结束再开始`
          : `已排队，执行器马上开始${run.level === 'physical' ? '动作级' : '只读级'}验收`),
    },
  );
  const cancel = useMutation((id: string) => api.post<AcceptanceRun>(`/acceptance-runs/${id}/cancel`), {
    invalidates: [key], onSuccess: () => toast.push('已取消排队中的验收'),
  });
  const waive = useMutation(
    (payload: Record<string, unknown>) => api.post<AcceptanceRun>(`/stations/${station.id}/adapter/acceptance/waive`, payload),
    { invalidates: [key, 'stations'], onSuccess: () => toast.push('已签名放行：放行记录已存档，工位恢复接指令') },
  );

  const gate = listing.data?.gate ?? adapter.acceptance;
  const accepted = gate?.accepted_config_version === adapter.config_version;
  const runs = listing.data?.runs ?? [];

  return (
    <div className="subsection">
      <div className="subsection-head">
        <b>接入验收</b>
        <div className="row">
          <button
            className="btn sm"
            disabled={request.pending || busy || !adapter.enabled}
            title="不会让设备动作：身份与方法目录、健康检查、契约声明、查询不存在的指令号"
            onClick={() => request.run({ level: 'readonly' }).catch((error) => toast.push(error.message))}
          >
            只读级验收
          </button>
          <button
            className="btn sm"
            disabled={request.pending || busy || !adapter.enabled}
            title="会让设备真的动作：要签名，并写明现场批准人"
            onClick={() => setPhysical(true)}
          >
            动作级验收…
          </button>
          {gate?.required ? (
            <button
              className="btn sm"
              disabled={waive.pending || busy}
              title="检查清单证明不了的设备（不支持状态查询、要现场摆位的动作）：现场核对后签名放行，放行记录存档"
              onClick={() => setWaiving(true)}
            >
              签名放行…
            </button>
          ) : null}
        </div>
      </div>
      {gate?.required ? (
        <div className="note warn">
          {gate.reason}：执行器{gate.required === 'physical' ? '已自动跑只读级；真实设备还要一次动作级验收（签名 + 现场批准）' : '自动跑只读级'}，
          通过之前这台设备不接动作指令。自报为模拟器的设备只读级就够；检查清单证明不了的设备可现场核对后签名放行。
        </div>
      ) : (
        <div className="small muted">
          {accepted ? `配置 v${adapter.config_version} 已通过接入验收。` : '当前配置没有欠验收（迁移前在用的配置不追溯）。'}
          报告存档作为上线证据；真实设备的故障项目（回执丢失）要在网络路径上注入，不在这里跑。
        </div>
      )}
      {runs.length ? (
        <table className="compact">
          <thead>
            <tr>
              <th>时间</th>
              <th>级别</th>
              <th>来由</th>
              <th>状态</th>
              <th>结论</th>
              <th>配置</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {runs.slice(0, 8).map((run) => (
              <tr key={run.id}>
                <td className="small mono">{clock(run.finished_at ?? run.started_at ?? run.created_at ?? '')}</td>
                <td className="small">
                  {run.level_label}
                  {run.faults ? <div className="tiny muted">+ 故障项目</div> : null}
                </td>
                <td className="small">
                  {run.trigger_label}
                  <div className="tiny muted">{run.requested_by}</div>
                </td>
                <td><Pill state={RUN_PILL[run.state] ?? 'neutral'} label={run.state_label} /></td>
                <td className="small">
                  {run.state === 'done' ? (
                    <>
                      <span className={run.ok ? 'ok-text' : 'bad-text'}>{run.ok ? '通过' : '不通过'}</span>
                      <div className="tiny muted">
                        通过 {run.counts.pass} · 不通过 {run.counts.fail} · 跳过 {run.counts.skip}
                        {run.simulator ? ' · 模拟器' : ''}
                      </div>
                    </>
                  ) : (
                    <span className="tiny muted">{run.error || '—'}</span>
                  )}
                </td>
                <td className="small mono">
                  v{run.config_version}
                  {run.template ? <div className="tiny muted">{run.template.code} r{run.template.revision}</div> : null}
                </td>
                <td className="row-end">
                  {run.state === 'done' ? (
                    <button className="btn sm" onClick={() => setViewing(run.id)}>报告</button>
                  ) : null}
                  {run.state === 'queued' ? (
                    <button className="btn sm" disabled={cancel.pending} onClick={() => cancel.run(run.id).catch((error) => toast.push(error.message))}>
                      取消
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="small muted">还没有验收记录。</div>
      )}
      {physical ? (
        <PhysicalRequest
          station={station}
          adapter={adapter}
          defaults={listing.data?.defaults}
          pending={request.pending}
          onClose={() => setPhysical(false)}
          onSubmit={async (payload) => {
            const signatureId = await sign('批准设备接入验收', station.id, ['批准设备接入验收'], adapter.config_version);
            if (!signatureId) return;
            await request.run({ ...payload, level: 'physical', signature_id: signatureId });
            setPhysical(false);
          }}
        />
      ) : null}
      {waiving ? (
        <WaiveRequest
          station={station}
          adapter={adapter}
          required={gate?.required_label ?? gate?.required ?? ''}
          pending={waive.pending}
          onClose={() => setWaiving(false)}
          onSubmit={async (reason) => {
            const signatureId = await sign('签名放行接入验收', station.id, ['签名放行接入验收'], adapter.config_version);
            if (!signatureId) return;
            await waive.run({ reason, signature_id: signatureId });
            setWaiving(false);
          }}
        />
      ) : null}
      {viewing ? <RunReport runId={viewing} onClose={() => setViewing(null)} /> : null}
    </div>
  );
}

function WaiveRequest({
  station, adapter, required, pending, onClose, onSubmit,
}: {
  station: StationRow;
  adapter: AdapterRow;
  required: string;
  pending: boolean;
  onClose: () => void;
  onSubmit: (reason: string) => Promise<void>;
}) {
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const submit = () => {
    if (reason.trim().length < 4) {
      setError('写明为什么检查清单证明不了、现场核对了什么、谁核对的');
      return;
    }
    setError('');
    onSubmit(reason.trim()).catch((caught) => setError(caught.message));
  };
  return (
    <Modal
      title={`签名放行接入验收 · ${station.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={pending} onClick={submit}>签名放行</button>
        </>
      }
    >
      <div className="note warn">
        放行的是配置 v{adapter.config_version}（{adapter.driver}）欠的{required}验收，不跑检查清单。只用于清单证明不了的设备：
        不支持按指令号查询、动作要现场摆位才能做。放行记录与签名一起存档，之后再改配置照样要重新验收。
      </div>
      <Field label="放行依据" hint="例如「设备不支持状态查询；现场负责人 张工 已手动走完一次完整循环并核对了回报」">
        <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

function PhysicalRequest({
  station, adapter, defaults, pending, onClose, onSubmit,
}: {
  station: StationRow;
  adapter: AdapterRow;
  defaults?: AcceptanceDefaults;
  pending: boolean;
  onClose: () => void;
  onSubmit: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const capabilities = Object.keys(station.limits ?? {}).sort();
  const first = defaults && capabilities.includes(defaults.capability) ? defaults.capability : capabilities[0] ?? '';
  const [capability, setCapability] = useState(first);
  const [params, setParams] = useState<Record<string, number | string>>(
    () => initialParams(first, station.limits?.[first] ?? {}, defaults).numeric,
  );
  const [extra, setExtra] = useState(() => initialParams(first, station.limits?.[first] ?? {}, defaults).extra);
  const [approval, setApproval] = useState('');
  const [faults, setFaults] = useState(false);
  const [error, setError] = useState('');
  const window = station.limits?.[capability] ?? {};

  const submit = () => {
    if (!approval.trim()) {
      setError('写明现场批准人与批准依据（DEC-02）：动作级验收会让设备真的动作');
      return;
    }
    if (Object.values(params).some((value) => value === '')) {
      setError('每个参数都要填，且落在工位能力极限里');
      return;
    }
    let structured: Record<string, unknown> = {};
    if (extra.trim()) {
      try {
        structured = JSON.parse(extra);
      } catch {
        setError('其他参数要是 JSON 对象，例如 {"from": {"location_id": "HOTEL-01/S01"}}');
        return;
      }
      if (!structured || typeof structured !== 'object' || Array.isArray(structured)) {
        setError('其他参数要是 JSON 对象');
        return;
      }
    }
    setError('');
    onSubmit({ capability, params: { ...structured, ...params }, approval: approval.trim(), faults })
      .catch((caught) => setError(caught.message));
  };

  return (
    <Modal
      title={`动作级接入验收 · ${station.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={pending} onClick={submit}>签名并排队</button>
        </>
      }
    >
      <div className="note warn">
        会让设备真的动作：正常完成一次、同一指令号重复提交、重建驱动后按指令号查回、保持、终止。设备上还有在动作的指令时，
        验收排队等它们结束；排队期间新的动作指令先不投递。验收指令的编号以 ACC- 开头。
      </div>
      <div className="grid cols-2">
        <Field label="验收用的能力">
          <select
            value={capability}
            onChange={(event) => {
              const next = initialParams(event.target.value, station.limits?.[event.target.value] ?? {}, defaults);
              setCapability(event.target.value);
              setParams(next.numeric);
              setExtra(next.extra);
            }}
          >
            {capabilities.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        </Field>
        <Field label="配置版本">
          <input value={`v${adapter.config_version} · ${adapter.driver}`} disabled />
        </Field>
      </div>
      <div className="grid cols-3">
        {Object.entries(window).map(([name, span]) =>
          isOptionWindow(span) ? (
            <Field key={name} label={`${name}（选项）`}>
              <select
                value={String(params[name] ?? '')}
                onChange={(event) => setParams((current) => ({ ...current, [name]: event.target.value }))}
              >
                {span.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <Field key={name} label={`${name}（${span[0]}–${span[1]}）`}>
              <NumberInput
                value={typeof params[name] === 'number' ? (params[name] as number) : ''}
                onChange={(value) => setParams((current) => ({ ...current, [name]: value }))}
              />
            </Field>
          ),
        )}
      </div>
      <Field
        label="其他参数（JSON，可空）"
        hint={`没有极限可核对的结构化参数，例如转运的起止位置；缺省取自${SOURCE_LABEL[defaults?.source ?? 'limits'] ?? '工位极限中点'}`}
      >
        <textarea rows={extra ? 5 : 2} className="mono" value={extra} onChange={(event) => setExtra(event.target.value)} />
      </Field>
      <Field label="现场批准" hint="谁批准、依据是什么，例如「现场负责人 张工，已确认设备空载、周边无人」">
        <textarea rows={2} value={approval} onChange={(event) => setApproval(event.target.value)} />
      </Field>
      <label className="check">
        <input type="checkbox" checked={faults} onChange={(event) => setFaults(event.target.checked)} />
        同时跑故障项目（回执丢失、设备忙、联锁、失联）：只对自报为模拟器、登记了控制口的设备生效，真实设备自动跳过
      </label>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

function RunReport({ runId, onClose }: { runId: string; onClose: () => void }) {
  const toast = useToast();
  const run = useQuery<AcceptanceRun>(`acceptance-run:${runId}`, () => api.get<AcceptanceRun>(`/acceptance-runs/${runId}`));
  const data = run.data;
  return (
    <Modal
      title={`验收报告 · ${data?.station_id ?? ''}`}
      wide
      onClose={onClose}
      footer={
        <button
          className="btn"
          disabled={!data}
          onClick={() =>
            api.download(`/acceptance-runs/${runId}/report.md`, `acceptance-${data?.station_id}-${runId}.md`)
              .catch((error) => toast.push(error.message))
          }
        >
          下载报告（Markdown）
        </button>
      }
    >
      {data ? (
        <>
          <div className="small">
            {data.level_label}{data.faults ? ' + 故障项目' : ''} · {data.trigger_label} · 申请人 {data.requested_by || '—'} ·
            驱动 <span className="mono">{data.driver}</span> · 配置 v{data.config_version}（摘要 <span className="mono">{data.config_digest}</span>）
            {data.template ? <> · 模板 {data.template.code} r{data.template.revision}</> : null}
          </div>
          <div className="small muted">
            设备自报：厂商 {data.identity.vendor || '—'} · 型号 {data.identity.reported_model || '—'} · 固件 {data.identity.firmware || '—'}
            {data.simulator ? ' · 模拟器' : ''}
            {data.approval ? <> · 现场批准：{data.approval}</> : null}
          </div>
          <table className="compact">
            <thead>
              <tr><th>项目</th><th>结论</th><th>说明</th></tr>
            </thead>
            <tbody>
              {(data.checks ?? []).map((check) => (
                <tr key={check.key}>
                  <td className="small">{check.label}</td>
                  <td><Pill state={CHECK_PILL[check.state] ?? 'neutral'} label={check.state_label} /></td>
                  <td className="small">{check.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <div className="muted">正在读取…</div>
      )}
    </Modal>
  );
}
