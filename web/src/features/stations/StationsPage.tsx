import { useEffect, useState } from 'react';

import { api } from '../../shared/api';
import { time } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import type { AdapterRow, AdapterTestResult, CapabilityRow, CommandRow, Recovery, StationRow } from '../../shared/types';
import { ConfirmDialog, Empty, Field, Modal, NumberInput, Panel, Pill, useToast } from '../../shared/ui';

export function StationsPage() {
  const { can } = useSession();
  const toast = useToast();
  const stations = useQuery<StationRow[]>('stations', () => api.get<StationRow[]>('/stations'), 15000);
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const commands = useQuery<CommandRow[]>('commands', () => api.get<CommandRow[]>('/commands?limit=30'), 10000);
  const [editing, setEditing] = useState<StationRow | null>(null);
  const [registering, setRegistering] = useState(false);
  const [addingStation, setAddingStation] = useState(false);
  const [editingStation, setEditingStation] = useState<StationRow | null>(null);
  const [editingAdapter, setEditingAdapter] = useState<StationRow | null>(null);
  const [editingCapability, setEditingCapability] = useState<CapabilityRow | null>(null);
  const [deletingCapability, setDeletingCapability] = useState<CapabilityRow | null>(null);

  const readiness = useMutation(
    (payload: { id: string; clean: boolean; status: string; row_version: number }) =>
      api.patch(`/stations/${payload.id}/readiness`, {
        clean: payload.clean, status: payload.status, row_version: payload.row_version,
      }),
    { invalidates: ['stations', 'dashboard', 'schedule'], onSuccess: () => toast.push('工位就绪状态已更新') },
  );

  const reconnect = useMutation((stationId: string) => api.post(`/stations/${stationId}/adapter/reconnect`), {
    invalidates: ['stations', 'dashboard', 'gate', 'audit'],
    onSuccess: () => toast.push('适配器已重连并对账最近检查点'),
  });

  const retireStation = useMutation(
    (payload: { id: string; retired: boolean }) =>
      api.post<{ broken_recipes: string[] }>(`/stations/${payload.id}/retire`, { retired: payload.retired }),
    {
      invalidates: ['stations', 'recipes', 'schedule', 'dashboard', 'audit'],
      onSuccess: (result) =>
        toast.push(
          result.broken_recipes.length
            ? `已更新；${result.broken_recipes.join('、')} 重校验不再通过`
            : '工位状态已更新，配方重校验无影响',
        ),
    },
  );

  const retireCapability = useMutation(
    (payload: { id: string; retired: boolean }) =>
      api.post(`/capabilities/${payload.id}/retire`, { retired: payload.retired }),
    {
      invalidates: ['capabilities', 'recipes', 'audit'],
      onSuccess: () => toast.push('能力状态已更新；已有配方与批次快照不受影响'),
    },
  );

  const removeCapability = useMutation((capabilityId: string) => api.remove(`/capabilities/${capabilityId}`), {
    invalidates: ['capabilities', 'stations', 'audit'],
    onSuccess: () => {
      toast.push('能力已删除');
      setDeletingCapability(null);
    },
  });

  const toManual = useMutation((commandId: string) => api.post(`/commands/${commandId}/manual-review`), {
    invalidates: ['commands', 'batches', 'dashboard', 'audit'],
    onSuccess: () => toast.push('已转人工核查：现场确认设备实态与检查点一致后再续跑'),
  });

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>工位与能力</h1>
          <div className="small muted">
            能力极限是配方校验与排程匹配的唯一数据源，修改需要电子签名并触发配方重校验
          </div>
        </div>
        <div className="row">
          {can('station.edit') ? (
            <button className="btn" onClick={() => setAddingStation(true)}>
              登记新工位
            </button>
          ) : null}
          {can('station.edit') ? (
            <button className="btn primary" onClick={() => setRegistering(true)}>
              登记新能力
            </button>
          ) : null}
        </div>
      </div>

      <Panel title="工位台账" flush>
        <table>
          <thead>
            <tr>
              <th>工位</th>
              <th>状态</th>
              <th className="num">样品位</th>
              <th>校准到期</th>
              <th>实现能力</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(stations.data ?? []).map((station) => (
              <tr key={station.id} className={station.retired ? 'retired-row' : undefined}>
                <td>
                  <b className="mono">{station.id}</b>
                  <div className="tiny muted">
                    {station.name} · {station.model} · 岛 {station.island}
                  </div>
                </td>
                <td>
                  {station.retired ? (
                    <Pill state="retired" label="已停用" />
                  ) : (
                    <Pill state={station.status} label={stationLabel(station.status)} />
                  )}
                  <div className="tiny muted">{station.clean ? '已清洗' : '未清洗'}</div>
                </td>
                <td className="num">{station.positions}</td>
                <td className="small mono">{station.cal_due}</td>
                <td className="small">
                  {Object.keys(station.limits).map((capability) => (
                    <span key={capability} className="tag mono">
                      {capability.replace('cap.', '')}
                    </span>
                  ))}
                </td>
                <td className="row-end">
                  {can('batch.control') ? (
                    <button
                      className="btn sm"
                      onClick={() =>
                        readiness
                          .run({ id: station.id, clean: !station.clean, status: station.status, row_version: station.row_version })
                          .catch((error) => toast.push(error.message))
                      }
                    >
                      {station.clean ? '标记未清洗' : '确认已清洗'}
                    </button>
                  ) : null}
                  {can('station.edit') ? (
                    <button className="btn sm" onClick={() => setEditingStation(station)}>
                      编辑台账
                    </button>
                  ) : null}
                  {can('station.edit') ? (
                    <button className="btn sm" onClick={() => setEditing(station)}>
                      编辑极限
                    </button>
                  ) : null}
                  {can('station.edit') ? (
                    <button
                      className={`btn sm${station.retired ? '' : ' danger'}`}
                      disabled={!station.retired && station.retire_blockers.length > 0}
                      title={station.retired ? '重新投入使用' : station.retire_blockers.join('；') || '停用后不再参与排程匹配'}
                      onClick={() =>
                        retireStation
                          .run({ id: station.id, retired: !station.retired })
                          .catch((error) => toast.push(error.message))
                      }
                    >
                      {station.retired ? '启用' : '停用'}
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="设备适配器" flush>
        <table>
          <thead>
            <tr>
              <th>工位</th>
              <th>模式</th>
              <th>协议</th>
              <th>连接</th>
              <th>心跳</th>
              <th className="num">去重次数</th>
              <th>当前指令</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(stations.data ?? [])
              .filter((station) => station.adapter)
              .map((station) => {
                const adapter = station.adapter!;
                return (
                  <tr key={station.id}>
                    <td className="mono">{station.id}</td>
                    <td>
                      <Pill
                        state={adapter.kind === 'real' ? 'running' : 'neutral'}
                        label={adapter.kind === 'real' ? '真实设备' : '模拟器'}
                      />
                    </td>
                    <td className="small">
                      {adapter.protocol}
                      <div className="tiny muted">v{adapter.version}</div>
                    </td>
                    <td>
                      <Pill
                        state={adapter.status === 'online' ? 'running' : adapter.status === 'degraded' ? 'paused' : adapter.status === 'disabled' ? 'retired' : 'fault'}
                        label={{ online: '在线', degraded: '降级', offline: '失联', disabled: '已停用' }[adapter.status] ?? adapter.status}
                      />
                      {adapter.site_interlock ? <div className="tiny bad-text">公共保护联锁</div> : null}
                      {!adapter.accepts_commands ? <div className="tiny warn-text">拒绝动作指令</div> : null}
                      {adapter.unsupported_note ? (
                        <div className="tiny muted">不支持：{adapter.unsupported_note}</div>
                      ) : null}
                    </td>
                    <td className="small mono">
                      {time(adapter.last_heartbeat)}
                      <div className="tiny muted">{adapter.heartbeat_age_sec.toFixed(0)} s 前</div>
                    </td>
                    <td className="num">{adapter.dedup_count}</td>
                    <td className="small mono">{adapter.current_command_id.slice(0, 8) || '—'}</td>
                    <td className="row-end">
                      {can('station.edit') ? (
                        <button className="btn sm" onClick={() => setEditingAdapter(station)}>
                          配置
                        </button>
                      ) : null}
                      {can('batch.control') && adapter.enabled && adapter.status !== 'online' ? (
                        <button
                          className="btn sm"
                          disabled={reconnect.pending}
                          onClick={() => reconnect.run(station.id).catch((error) => toast.push(error.message))}
                        >
                          重连
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
          </tbody>
        </table>
        <div className="panel-body small muted">
          “模拟器”只验证系统流程，不代表对应协议已经接入真实设备。心跳超过 5 s 标记降级，超过 5 min
          全站执行门锁定。重连只是重新握手并对账最近检查点，在途批次要不要续跑仍由恢复评估决定。
        </div>
      </Panel>

      <Panel title="指令对账" flush>
        {commands.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>指令</th>
                <th>批次</th>
                <th>工位</th>
                <th>类型</th>
                <th>状态</th>
                <th>更新</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {commands.data.map((command) => (
                <tr key={command.id}>
                  <td className="mono small">{command.id.slice(0, 8)}</td>
                  <td className="mono small">{command.batch_id}</td>
                  <td className="mono">{command.station_id}</td>
                  <td className="small">
                    第 {command.step_index + 1} 步 {command.type}
                    {command.error ? <div className="tiny bad-text">{command.error}</div> : null}
                  </td>
                  <td>
                    <Pill state={command.state} label={commandLabel(command.state)} />
                  </td>
                  <td className="small mono">{time(command.updated_at ?? command.created_at)}</td>
                  <td className="row-end">
                    {command.state === 'unknown' && can('batch.control') ? (
                      <button
                        className="btn sm"
                        disabled={toManual.pending}
                        onClick={() => toManual.run(command.id).catch((error) => toast.push(error.message))}
                      >
                        转人工核查
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>还没有指令记录</Empty>
        )}
        <div className="panel-body small muted">
          指令七态：已发送 → 设备已接受 → 执行中 → 已完成 / 结果未知。结果未知不自动重试，进入人工核查。
        </div>
      </Panel>

      <Panel title="能力字典与恢复规则" flush>
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
            {(capabilities.data ?? []).map((capability) => (
              <tr key={capability.id} className={capability.retired ? 'retired-row' : undefined}>
                <td>
                  {capability.name}
                  {capability.retired ? <Pill state="retired" label="已停用" /> : null}
                  <div className="tiny muted mono">{capability.id}</div>
                </td>
                <td className="small">{Object.values(capability.params).join(' · ') || '无参数'}</td>
                <td className="small">
                  {capability.recovery.pausable ? `≤ ${capability.recovery.maxHoldMin} min` : '不可保持'}
                  {capability.recovery.hold ? <div className="tiny muted">{capability.recovery.hold}</div> : null}
                </td>
                <td className="small">
                  {capability.recovery.retryable ? '可重试' : '不可重试'}
                  {capability.recovery.sideEffect ? (
                    <div className="tiny muted">{capability.recovery.sideEffect}</div>
                  ) : null}
                </td>
                <td className="small muted">{capability.recovery.verify?.join('、') || '无'}</td>
                <td className="small mono">
                  {capability.stations.join('、') || '无'}
                  {capability.recipes.length ? (
                    <div className="tiny muted">{capability.recipes.length} 个配方在用</div>
                  ) : null}
                </td>
                <td className="row-end">
                  {can('station.edit') ? (
                    <>
                      <button className="btn sm" onClick={() => setEditingCapability(capability)}>
                        编辑
                      </button>
                      <button
                        className="btn sm"
                        title={capability.retired ? '恢复可选' : '新配方步骤不能再选它，已有配方不受影响'}
                        onClick={() =>
                          retireCapability
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
                        onClick={() => setDeletingCapability(capability)}
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
      </Panel>

      {editing ? (
        <LimitsEditor station={editing} capabilities={capabilities.data ?? []} onClose={() => setEditing(null)} />
      ) : null}
      {registering ? (
        <CapabilityForm stations={stations.data ?? []} onClose={() => setRegistering(false)} />
      ) : null}
      {addingStation ? (
        <StationForm capabilities={capabilities.data ?? []} onClose={() => setAddingStation(false)} />
      ) : null}
      {editingStation ? (
        <StationLedgerForm station={editingStation} onClose={() => setEditingStation(null)} />
      ) : null}
      {editingAdapter ? (
        <AdapterEditor station={editingAdapter} onClose={() => setEditingAdapter(null)} />
      ) : null}
      {editingCapability ? (
        <CapabilityEditForm capability={editingCapability} onClose={() => setEditingCapability(null)} />
      ) : null}
      {deletingCapability ? (
        <ConfirmDialog
          title={`删除能力 · ${deletingCapability.name}`}
          danger
          confirmLabel="删除"
          pending={removeCapability.pending}
          error={removeCapability.error?.message}
          onClose={() => setDeletingCapability(null)}
          onConfirm={() => removeCapability.run(deletingCapability.id).catch(() => undefined)}
        >
          <div className="note warn">
            <span className="mono">{deletingCapability.id}</span> 没有工位实现、也没有配方引用，可以从字典里移除。
            有引用的能力请改用「停用」。
          </div>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function AdapterEditor({ station, onClose }: { station: StationRow; onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  const detail = useQuery<AdapterRow>(
    `adapter-detail-${station.id}`,
    () => api.get<AdapterRow>(`/stations/${station.id}/adapter`),
  );
  const [draft, setDraft] = useState<AdapterRow | null>(null);
  const [configText, setConfigText] = useState('');
  const [error, setError] = useState('');
  const [testResult, setTestResult] = useState<AdapterTestResult | null>(null);

  useEffect(() => {
    if (!detail.data) return;
    setDraft(detail.data);
    setConfigText(JSON.stringify(detail.data.config ?? {}, null, 2));
  }, [detail.data]);

  const save = useMutation(
    (payload: Record<string, unknown>) => api.patch<AdapterRow>(`/stations/${station.id}/adapter`, payload),
    {
      invalidates: ['stations', `adapter-detail-${station.id}`, 'gate', 'audit'],
      onSuccess: () => {
        toast.push('适配器配置已保存；连接状态已清除，请测试后再重连');
        onClose();
      },
    },
  );
  const test = useMutation(
    () => api.post<AdapterTestResult>(`/stations/${station.id}/adapter/test`),
    {
      onSuccess: (result) => {
        setTestResult(result);
        toast.push(result.contract.kind === 'simulation' ? '模拟器健康检查通过（不代表真实设备）' : '真实设备健康检查通过');
      },
    },
  );

  const update = <K extends keyof AdapterRow>(key: K, value: AdapterRow[K]) =>
    setDraft((current) => current ? { ...current, [key]: value } : current);

  const applyHttpGatewayTemplate = () => {
    setDraft((current) => current ? {
      ...current,
      kind: 'real',
      driver: 'http_json_v1',
      protocol: 'HTTPS JSON',
      version: '1.0',
      capabilities: { ...current.capabilities, query: true, dedup: true },
    } : current);
    setConfigText(JSON.stringify({
      base_url: 'https://instrument-gateway.lab.internal/api/v1',
      verify_tls: true,
      connect_timeout_sec: 3,
      request_timeout_sec: 10,
      expected_device_id: station.id,
      paths: {
        health: '/health',
        submit: '/commands',
        query: '/commands/{command_id}',
        hold: '/commands/{command_id}/hold',
        abort: '/commands/{command_id}/abort',
      },
      idempotency_header: 'Idempotency-Key',
    }, null, 2));
  };

  const submit = async () => {
    if (!draft) return;
    let config: Record<string, unknown>;
    try {
      const parsed = JSON.parse(configText || '{}');
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('配置必须是 JSON 对象');
      config = parsed as Record<string, unknown>;
    } catch (caught) {
      setError(caught instanceof Error ? `配置 JSON 无效：${caught.message}` : '配置 JSON 无效');
      return;
    }
    if (draft.kind === 'real' && (!draft.driver.trim() || draft.driver === 'simulation')) {
      setError('真实设备必须填写已在后端注册的驱动键');
      return;
    }
    setError('');
    const signatureId = await sign('修改设备适配器', station.id, ['设备集成配置变更批准'], draft.row_version);
    if (!signatureId) return;
    await save.run({
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
      row_version: draft.row_version,
      signature_id: signatureId,
    }).catch((caught) => setError(caught.message));
  };

  if (detail.loading && !draft) {
    return <Modal title={`适配器配置 · ${station.id}`} onClose={onClose}><div className="muted">正在读取受控配置…</div></Modal>;
  }
  if (detail.error || !draft) {
    return <Modal title={`适配器配置 · ${station.id}`} onClose={onClose}><div className="note bad">{detail.error?.message ?? '适配器不存在'}</div></Modal>;
  }

  return (
    <Modal
      title={`适配器配置 · ${station.id}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn" disabled={test.pending} onClick={() => test.run().catch((caught) => setError(caught.message))}>
            {test.pending ? '测试中…' : '测试当前已保存配置'}
          </button>
          <button className="btn primary" disabled={save.pending} onClick={submit}>签名并保存</button>
        </>
      }
    >
      <div className="note warn">
        保存会递增配置版本并强制离线，避免旧连接继续被当作有效。密钥原文不得写进 JSON，只能填写密钥管理器引用。
        <div><button type="button" className="btn small" onClick={applyHttpGatewayTemplate}>填入 HTTPS JSON 网关模板</button></div>
      </div>
      <div className="grid cols-3">
        <Field label="模式">
          <select value={draft.kind} onChange={(event) => update('kind', event.target.value as AdapterRow['kind'])}>
            <option value="simulation">模拟器</option>
            <option value="real">真实设备</option>
          </select>
        </Field>
        <Field label="驱动键" hint="已内置 http_json_v1；其他键必须先在后端注册">
          <input
            className="mono"
            value={draft.kind === 'simulation' ? 'simulation' : draft.driver}
            disabled={draft.kind === 'simulation'}
            onChange={(event) => update('driver', event.target.value)}
          />
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
          <input value={draft.protocol} placeholder="SiLA 2 / OPC UA / Modbus TCP" onChange={(event) => update('protocol', event.target.value)} />
        </Field>
        <Field label="协议/驱动版本">
          <input value={draft.version} onChange={(event) => update('version', event.target.value)} />
        </Field>
      </div>
      <Field label="连接配置 JSON" hint='http_json_v1 示例：{"base_url":"https://gateway/api/v1","verify_tls":true,"expected_device_id":"ST-01"}'>
        <textarea className="mono" rows={8} value={configText} onChange={(event) => setConfigText(event.target.value)} />
      </Field>
      <Field label="凭据引用" hint="只接受 vault://、env://、file://；不要填写密码、token 或私钥原文">
        <input className="mono" value={draft.credential_ref} placeholder="vault://ilcs/devices/ST-01" onChange={(event) => update('credential_ref', event.target.value)} />
      </Field>
      <Field label="设备动作能力">
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
      <div className="small muted">当前配置 v{draft.config_version} · 行版本 v{draft.row_version} · 凭据{draft.credential_configured ? '已配置' : '未配置'}</div>
      {testResult ? <div className="note">健康检查结果：<span className="mono">{JSON.stringify(testResult.health)}</span></div> : null}
      {error || test.error ? <div className="note bad">{error || test.error?.message}</div> : null}
    </Modal>
  );
}

/* 结构化极限编辑。一行一个参数，只填上下限两个数；未列出的参数视为该工位不能承接。
   区间是配方校验与排程匹配的唯一判据，所以这里改完要签名，并当场把受影响的配方列出来。 */
function LimitsEditor({
  station,
  capabilities,
  onClose,
}: {
  station: StationRow;
  capabilities: CapabilityRow[];
  onClose: () => void;
}) {
  const toast = useToast();
  const { sign } = useSignature();
  const [limits, setLimits] = useState<Record<string, Record<string, [number | '', number | '']>>>(
    () => JSON.parse(JSON.stringify(station.limits ?? {})),
  );
  const [adding, setAdding] = useState('');
  const [error, setError] = useState('');

  const save = useMutation(
    (payload: { limits: unknown; signature_id: string }) =>
      api.patch<{ broken_recipes: string[] }>(`/stations/${station.id}/limits`, {
        ...payload, row_version: station.row_version,
      }),
    {
      invalidates: ['stations', 'recipes', 'schedule', 'dashboard', 'audit'],
      onSuccess: (result) => {
        toast.push(
          result.broken_recipes.length
            ? `已保存；${result.broken_recipes.join('、')} 校验不再通过，已标记需修订`
            : '已保存并重新校验全部引用配方',
        );
        onClose();
      },
    },
  );

  const setBound = (capability: string, param: string, edge: 0 | 1, value: number | '') =>
    setLimits((current) => {
      const next = JSON.parse(JSON.stringify(current)) as typeof current;
      next[capability][param][edge] = value;
      return next;
    });

  const addCapability = (capabilityId: string) => {
    const definition = capabilities.find((row) => row.id === capabilityId);
    if (!definition || limits[capabilityId]) return;
    setLimits((current) => ({
      ...current,
      [capabilityId]: Object.fromEntries(Object.keys(definition.params).map((key) => [key, [0, 100]])),
    }));
    setAdding('');
  };

  const invalidRows = Object.entries(limits).flatMap(([capability, params]) =>
    Object.entries(params)
      .filter(([, window]) => !(typeof window[0] === 'number' && typeof window[1] === 'number' && window[0] < window[1]))
      .map(([param]) => `${capability}.${param}`),
  );

  const submit = async () => {
    if (invalidRows.length) {
      setError(`下限必须小于上限：${invalidRows.join('、')}`);
      return;
    }
    setError('');
    const signatureId = await sign('修改能力极限', station.id, ['工程变更批准']);
    if (!signatureId) return;
    await save.run({ limits, signature_id: signatureId }).catch((caught) => setError(caught.message));
  };

  const missing = capabilities.filter((row) => !limits[row.id]);

  return (
    <Modal
      title={`能力极限 · ${station.id}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={save.pending || !!invalidRows.length} onClick={submit}>
            签名并保存
          </button>
        </>
      }
    >
      <div className="note warn">
        工位未定义的参数视为不能承接该步骤。保存后服务端重校验所有引用这些能力的配方，
        已发布但不再通过的版本进入「需修订」，不能再创建批次。
      </div>

      {Object.entries(limits).map(([capabilityId, params]) => {
        const definition = capabilities.find((row) => row.id === capabilityId);
        return (
          <div key={capabilityId}>
            <div className="small" style={{ marginBottom: 6 }}>
              <b>{definition?.name ?? capabilityId}</b> <span className="tiny muted mono">{capabilityId}</span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>参数</th>
                  <th className="num">下限</th>
                  <th className="num">上限</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(params).map(([param, window]) => {
                  const bad = !(typeof window[0] === 'number' && typeof window[1] === 'number' && window[0] < window[1]);
                  return (
                    <tr key={param}>
                      <td className="small">
                        {definition?.params?.[param] ?? param}
                        <div className="tiny muted mono">{param}</div>
                      </td>
                      <td className="num">
                        <NumberInput
                          value={window[0]}
                          invalid={bad}
                          ariaLabel={`${param} 下限`}
                          onChange={(next) => setBound(capabilityId, param, 0, next)}
                        />
                      </td>
                      <td className="num">
                        <NumberInput
                          value={window[1]}
                          invalid={bad}
                          ariaLabel={`${param} 上限`}
                          onChange={(next) => setBound(capabilityId, param, 1, next)}
                        />
                      </td>
                    </tr>
                  );
                })}
                {Object.keys(params).length ? null : (
                  <tr>
                    <td colSpan={3} className="muted small">
                      该能力无参数，登记即视为可承接
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        );
      })}

      {missing.length ? (
        <Field label="为本工位增加一项能力">
          <div className="row">
            <select value={adding} onChange={(event) => setAdding(event.target.value)}>
              <option value="">选择能力</option>
              {missing.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name}
                </option>
              ))}
            </select>
            <button className="btn sm" disabled={!adding} onClick={() => addCapability(adding)}>
              添加
            </button>
          </div>
        </Field>
      ) : null}

      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/* 登记新能力：参数定义 + 恢复规则 + 实现工位。恢复规则写在能力上而不是配方上，
   因为「能不能保持、能不能重试」是设备物理属性，配方无权覆盖。 */
function CapabilityForm({ stations, onClose }: { stations: StationRow[]; onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  const [id, setId] = useState('cap.');
  const [name, setName] = useState('');
  const [params, setParams] = useState<{ key: string; label: string }[]>([{ key: '', label: '' }]);
  const [recovery, setRecovery] = useState<Recovery>({
    pausable: true, maxHoldMin: 30, hold: '', retryable: false, sideEffect: '', verify: [],
  });
  const [verifyText, setVerifyText] = useState('');
  const [picked, setPicked] = useState<string[]>([]);
  const [error, setError] = useState('');

  const create = useMutation((payload: Record<string, unknown>) => api.post('/capabilities', payload), {
    invalidates: ['capabilities', 'stations', 'recipes', 'audit'],
    onSuccess: () => {
      toast.push('能力已登记；实现工位的参数极限先给默认区间，请随后按实际标定修改');
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
        params: Object.fromEntries(validParams.map((row) => [row.key.trim(), row.label.trim() || row.key.trim()])),
        recovery: {
          ...recovery,
          verify: verifyText.split(/[、,，\s]+/).map((item) => item.trim()).filter(Boolean),
        },
        stations: picked,
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
        能力是配方步骤的绑定对象。参数定义决定配方里能填哪些字段，恢复规则由能力继承到每一个引用它的步骤。
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
          参数定义（键用于配方与指令，标签用于界面显示，建议带单位）
        </div>
        <table>
          <thead>
            <tr>
              <th>键</th>
              <th>标签</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {params.map((row, index) => (
              <tr key={index}>
                <td>
                  <input
                    className="mono"
                    value={row.key}
                    aria-label={`参数 ${index + 1} 键`}
                    placeholder="power"
                    onChange={(event) =>
                      setParams((current) =>
                        current.map((item, order) => (order === index ? { ...item, key: event.target.value } : item)),
                      )
                    }
                  />
                </td>
                <td>
                  <input
                    value={row.label}
                    aria-label={`参数 ${index + 1} 标签`}
                    placeholder="超声功率 W"
                    onChange={(event) =>
                      setParams((current) =>
                        current.map((item, order) => (order === index ? { ...item, label: event.target.value } : item)),
                      )
                    }
                  />
                </td>
                <td className="row-end">
                  <button
                    className="btn sm"
                    aria-label={`删除参数 ${index + 1}`}
                    onClick={() => setParams((current) => current.filter((_, order) => order !== index))}
                  >
                    删
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button
          className="btn sm"
          style={{ marginTop: 8 }}
          onClick={() => setParams((current) => [...current, { key: '', label: '' }])}
        >
          添加参数
        </button>
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

      <Field label="实现工位" hint="勾选后按参数默认区间 [0, 100] 写入，请随后按实际标定修改">
        <div className="row">
          {stations.map((station) => (
            <label key={station.id} className="check">
              <input
                type="checkbox"
                checked={picked.includes(station.id)}
                onChange={(event) =>
                  setPicked((current) =>
                    event.target.checked ? [...current, station.id] : current.filter((item) => item !== station.id),
                  )
                }
              />
              <span className="mono">{station.id}</span>
            </label>
          ))}
        </div>
      </Field>

      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/* 登记新工位。能力极限一并写入并立刻重校验受影响的配方，所以要签名。 */
function StationForm({ capabilities, onClose }: { capabilities: CapabilityRow[]; onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  const [form, setForm] = useState({ id: 'ST-', name: '', island: 1, model: '', cal_due: '', positions: 1 });
  const [protocol, setProtocol] = useState('');
  const [adapterVersion, setAdapterVersion] = useState('');
  const [adapterKind, setAdapterKind] = useState<'simulation' | 'real'>('simulation');
  const [adapterDriver, setAdapterDriver] = useState('simulation');
  const [adapterConfig, setAdapterConfig] = useState('{}');
  const [credentialRef, setCredentialRef] = useState('');
  const [features, setFeatures] = useState({ hold: true, abort: true, query: true, dedup: true });
  const [picked, setPicked] = useState<string[]>([]);
  const [error, setError] = useState('');

  const create = useMutation((payload: Record<string, unknown>) => api.post('/stations', payload), {
    invalidates: ['stations', 'capabilities', 'recipes', 'schedule', 'dashboard', 'audit'],
    onSuccess: () => {
      toast.push('工位已登记；能力极限先给默认区间，请按实际标定修改');
      onClose();
    },
  });

  const ready = /^[A-Za-z0-9-]{3,}$/.test(form.id) && form.name.trim().length > 0;

  const applyHttpGatewayTemplate = () => {
    setProtocol('HTTPS JSON');
    setAdapterKind('real');
    setAdapterDriver('http_json_v1');
    setAdapterVersion('1.0');
    setFeatures({ hold: true, abort: true, query: true, dedup: true });
    setAdapterConfig(JSON.stringify({
      base_url: 'https://instrument-gateway.lab.internal/api/v1',
      verify_tls: true,
      connect_timeout_sec: 3,
      request_timeout_sec: 10,
      expected_device_id: form.id,
      paths: {
        health: '/health', submit: '/commands', query: '/commands/{command_id}',
        hold: '/commands/{command_id}/hold', abort: '/commands/{command_id}/abort',
      },
      idempotency_header: 'Idempotency-Key',
    }, null, 2));
  };

  const submit = async () => {
    if (!ready) {
      setError('标识至少 3 位（字母、数字、连字符），名称不能为空');
      return;
    }
    setError('');
    let parsedAdapterConfig: Record<string, unknown> = {};
    if (protocol) {
      try {
        const parsed = JSON.parse(adapterConfig || '{}');
        if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('必须是 JSON 对象');
        parsedAdapterConfig = parsed as Record<string, unknown>;
      } catch (caught) {
        setError(caught instanceof Error ? `适配器配置 JSON 无效：${caught.message}` : '适配器配置 JSON 无效');
        return;
      }
      if (adapterKind === 'real' && (!adapterDriver.trim() || adapterDriver === 'simulation')) {
        setError('真实设备必须填写已在后端注册的驱动键');
        return;
      }
    }
    const signatureId = await sign('登记新工位', form.id, ['工程变更批准']);
    if (!signatureId) return;
    const limits = Object.fromEntries(
      picked.map((capabilityId) => [
        capabilityId,
        Object.fromEntries(Object.keys(capabilities.find((c) => c.id === capabilityId)?.params ?? {}).map((k) => [k, [0, 100]])),
      ]),
    );
    await create
      .run({
        ...form, limits, protocol, adapter_version: adapterVersion, adapter_kind: adapterKind,
        adapter_driver: adapterKind === 'simulation' ? 'simulation' : adapterDriver.trim(),
        adapter_config: parsedAdapterConfig, credential_ref: credentialRef.trim(),
        supports_hold: features.hold, supports_abort: features.abort,
        supports_query: features.query, supports_dedup: features.dedup,
        signature_id: signatureId,
      })
      .catch((caught) => setError(caught.message));
  };

  return (
    <Modal
      title="登记新工位"
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={create.pending} onClick={submit}>签名并登记</button>
        </>
      }
    >
      <div className="note">
        工位是排程匹配的落点。勾选的能力先按 [0, 100] 写入极限占位，登记后请到「编辑极限」按实际标定改。
        <div><button type="button" className="btn small" onClick={applyHttpGatewayTemplate}>使用 HTTPS JSON 真实设备模板</button></div>
      </div>
      <div className="grid cols-2">
        <Field label="标识" hint="例如 ST-08，登记后不可更改">
          <input className="mono" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} />
        </Field>
        <Field label="名称">
          <input value={form.name} placeholder="例如：超声分散站" onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
      </div>
      <div className="grid cols-3">
        <Field label="功能岛">
          <NumberInput value={form.island} ariaLabel="功能岛" onChange={(v) => setForm({ ...form, island: Number(v) || 0 })} />
        </Field>
        <Field label="样品位">
          <NumberInput value={form.positions} ariaLabel="样品位" invalid={!(form.positions >= 1)} onChange={(v) => setForm({ ...form, positions: Number(v) || 1 })} />
        </Field>
        <Field label="校准到期">
          <input value={form.cal_due} placeholder="2027-01-01" onChange={(e) => setForm({ ...form, cal_due: e.target.value })} />
        </Field>
      </div>
      <div className="grid cols-2">
        <Field label="型号">
          <input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} />
        </Field>
        <Field label="适配器协议" hint="留空表示暂不登记适配器；登记后等首次心跳才算在线">
          <input value={protocol} placeholder="SiLA 2" onChange={(e) => setProtocol(e.target.value)} />
        </Field>
      </div>
      {protocol ? (
        <>
          <div className="grid cols-3">
            <Field label="适配器模式" hint="模拟器不能作为真实设备验收证据">
              <select
                value={adapterKind}
                onChange={(e) => {
                  const kind = e.target.value as 'simulation' | 'real';
                  setAdapterKind(kind);
                  if (kind === 'simulation') setAdapterDriver('simulation');
                  else if (adapterDriver === 'simulation') setAdapterDriver('http_json_v1');
                }}
              >
                <option value="simulation">模拟器</option>
                <option value="real">真实设备</option>
              </select>
            </Field>
            <Field label="驱动键" hint="已内置 http_json_v1；其他键必须先在后端注册">
              <input
                className="mono"
                value={adapterKind === 'simulation' ? 'simulation' : adapterDriver}
                disabled={adapterKind === 'simulation'}
                placeholder="http_json_v1"
                onChange={(e) => setAdapterDriver(e.target.value)}
              />
            </Field>
            <Field label="协议/驱动版本">
              <input value={adapterVersion} placeholder="1.0" onChange={(e) => setAdapterVersion(e.target.value)} />
            </Field>
          </div>
          <Field label="连接配置 JSON" hint='只填非秘密参数，例如 {"host":"10.0.0.20","port":502,"unit_id":1}'>
            <textarea className="mono" rows={5} value={adapterConfig} onChange={(e) => setAdapterConfig(e.target.value)} />
          </Field>
          <Field label="凭据引用" hint="可选，只接受 vault://、env://、file://；禁止保存密码原文">
            <input className="mono" value={credentialRef} placeholder="vault://ilcs/devices/ST-08" onChange={(e) => setCredentialRef(e.target.value)} />
          </Field>
          <Field label="设备能力声明" hint="设备不支持的动作会在执行界面禁用">
            <div className="row">
              {([
                ['hold', '保持'], ['abort', '终止'], ['query', '按指令查询'], ['dedup', '设备端去重'],
              ] as const).map(([key, label]) => (
                <label className="check" key={key}>
                  <input
                    type="checkbox"
                    checked={features[key]}
                    onChange={(e) => setFeatures((current) => ({ ...current, [key]: e.target.checked }))}
                  />
                  {label}
                </label>
              ))}
            </div>
          </Field>
          {adapterKind === 'real' ? (
            <div className="note warn">
              真实模式只表示要调用已登记驱动；没有对应驱动实现时执行器会拒绝启动该设备命令，不会回落到模拟器。
            </div>
          ) : null}
        </>
      ) : null}
      <Field label="实现哪些能力">
        <div className="row">
          {capabilities.filter((c) => !c.retired).map((capability) => (
            <label key={capability.id} className="check">
              <input
                type="checkbox"
                checked={picked.includes(capability.id)}
                onChange={(e) =>
                  setPicked((current) =>
                    e.target.checked ? [...current, capability.id] : current.filter((x) => x !== capability.id),
                  )
                }
              />
              {capability.name}
            </label>
          ))}
        </div>
      </Field>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/* 台账信息不影响排程判据，所以不要签名；能力极限走另一条要签名的路。 */
function StationLedgerForm({ station, onClose }: { station: StationRow; onClose: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({
    name: station.name, model: station.model, island: station.island,
    positions: station.positions, cal_due: station.cal_due,
  });
  const [error, setError] = useState('');

  const save = useMutation((payload: Record<string, unknown>) => api.patch(`/stations/${station.id}`, payload), {
    invalidates: ['stations', 'dashboard', 'schedule', 'audit'],
    onSuccess: () => {
      toast.push('工位台账已更新');
      onClose();
    },
  });

  return (
    <Modal
      title={`编辑台账 · ${station.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={save.pending} onClick={() => save.run({ ...form, row_version: station.row_version }).catch((c) => setError(c.message))}>
            保存
          </button>
        </>
      }
    >
      <div className="note">
        这里只改台账信息。能力极限是排程与配方校验的判据，改动要签名并触发重校验，请用「编辑极限」。
      </div>
      <Field label="名称">
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </Field>
      <div className="grid cols-2">
        <Field label="型号">
          <input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} />
        </Field>
        <Field label="功能岛">
          <NumberInput value={form.island} ariaLabel="功能岛" onChange={(v) => setForm({ ...form, island: Number(v) || 0 })} />
        </Field>
      </div>
      <div className="grid cols-2">
        <Field label="样品位">
          <NumberInput value={form.positions} ariaLabel="样品位" invalid={!(form.positions >= 1)} onChange={(v) => setForm({ ...form, positions: Number(v) || 1 })} />
        </Field>
        <Field label="校准到期">
          <input value={form.cal_due} onChange={(e) => setForm({ ...form, cal_due: e.target.value })} />
        </Field>
      </div>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/* 改能力定义。增删参数会改变所有引用配方的校验结果，所以签名并当场报告影响面。 */
function CapabilityEditForm({ capability, onClose }: { capability: CapabilityRow; onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  const [name, setName] = useState(capability.name);
  const [params, setParams] = useState<{ key: string; label: string }[]>(
    () => Object.entries(capability.params ?? {}).map(([key, label]) => ({ key, label })),
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
            : '已保存并重新校验全部引用配方',
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
        params: Object.fromEntries(params.filter((p) => p.key.trim()).map((p) => [p.key.trim(), p.label.trim() || p.key.trim()])),
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
          {capability.recipes.length} 个配方的步骤在用它（{capability.recipes.slice(0, 5).join('、')}）。
          改参数定义会立刻重算它们的校验结果。
        </div>
      ) : null}
      {removed.length ? (
        <div className="note bad">
          将移除参数 <span className="mono">{removed.join('、')}</span>：引用它的配方步骤会变成「参数不属于该能力」，
          各工位极限里的对应条目也会一并清掉。
        </div>
      ) : null}

      <Field label="名称">
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </Field>

      <div>
        <div className="small muted" style={{ marginBottom: 6 }}>参数定义</div>
        <table>
          <thead><tr><th>键</th><th>标签</th><th /></tr></thead>
          <tbody>
            {params.map((row, index) => (
              <tr key={index}>
                <td>
                  <input className="mono" value={row.key} aria-label={`参数 ${index + 1} 键`}
                    onChange={(e) => setParams((c) => c.map((x, i) => (i === index ? { ...x, key: e.target.value } : x)))} />
                </td>
                <td>
                  <input value={row.label} aria-label={`参数 ${index + 1} 标签`}
                    onChange={(e) => setParams((c) => c.map((x, i) => (i === index ? { ...x, label: e.target.value } : x)))} />
                </td>
                <td className="row-end">
                  <button className="btn sm" aria-label={`删除参数 ${index + 1}`}
                    onClick={() => setParams((c) => c.filter((_, i) => i !== index))}>删</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="btn sm" style={{ marginTop: 8 }} onClick={() => setParams((c) => [...c, { key: '', label: '' }])}>
          添加参数
        </button>
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

function stationLabel(status: string): string {
  return { idle: '空闲', running: '运行', fault: '故障', offline: '离线' }[status] ?? status;
}

function commandLabel(state: string): string {
  return (
    {
      sent: '已发送', accepted: '设备已接受', running: '执行中', done: '已完成',
      unknown: '结果未知', manual: '人工核查中', rejected: '设备拒绝',
    }[state] ?? state
  );
}
