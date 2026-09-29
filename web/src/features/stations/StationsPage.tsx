import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { day, time } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import { CHANNEL_UNIT_LABEL } from '../../shared/types';
import type { CapabilityRow, ChannelUnit, StationAsset, StationRow } from '../../shared/types';
import { Field, Modal, NumberInput, Panel, Pill, useToast } from '../../shared/ui';
import { AdapterEditor } from './AdapterEditor';

/* 工位配置：系统里的执行位置能接什么活（能力极限）、同时接几份（通道）、怎么连设备（适配器）。

   以静态配置为主。实物属性——型号、序列号、校准、资产状态与总容量——归「仪器设备」，这里只读显示关联资产的结论；
   清洗确认、结果未知指令转人工核查、适配器重连是现场操作，在「现场监控」做；能力本身的定义在「能力字典」。 */

const CHANNELS_HINT =
  '按批次计：同一时刻能同时跑几个批次的设备步骤，一个批次的一个设备步骤占 1 个，与样本数无关。' +
  '按样本计：填物理通道数，批次里每个样本各占 1 个（8 通道柜同时跑 8 颗：一批 8 颗，或 5 颗加 3 颗）。不能超过所属资产的容量';

const CHANNEL_UNIT_HINT =
  '一颗电芯占一个物理通道的充放电柜选「按样本」；工位上还有未结束批次的时间窗时不能改';

function ChannelUnitSelect({ value, onChange }: { value: ChannelUnit; onChange: (value: ChannelUnit) => void }) {
  return (
    <select value={value} onChange={(event) => onChange(event.target.value as ChannelUnit)}>
      {(Object.keys(CHANNEL_UNIT_LABEL) as ChannelUnit[]).map((key) => (
        <option key={key} value={key}>{CHANNEL_UNIT_LABEL[key]}</option>
      ))}
    </select>
  );
}

const ADAPTER_STATUS: Record<string, [string, string]> = {
  online: ['running', '在线'], degraded: ['paused', '降级'], stale: ['fault', '心跳超时'],
  offline: ['fault', '失联'], disabled: ['retired', '已停用'], acceptance: ['paused', '待接入验收'],
};

const ASSET_STATE: Record<string, string> = { active: '正常', maintenance: '维护中', retired: '已退役' };

export function StationsPage() {
  const { can } = useSession();
  const toast = useToast();
  const stations = useQuery<StationRow[]>('stations', () => api.get<StationRow[]>('/stations'), 15000);
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const [editing, setEditing] = useState<StationRow | null>(null);
  const [addingStation, setAddingStation] = useState(false);
  const [editingStation, setEditingStation] = useState<StationRow | null>(null);
  const [editingAdapter, setEditingAdapter] = useState<StationRow | null>(null);

  const retireStation = useMutation(
    (payload: { id: string; retired: boolean }) =>
      api.post<{ broken_recipes: string[] }>(`/stations/${payload.id}/retire`, { retired: payload.retired }),
    {
      invalidates: ['stations', 'recipes', 'schedule', 'dashboard', 'audit'],
      onSuccess: (result) =>
        toast.push(
          result.broken_recipes.length
            ? `已更新；${result.broken_recipes.join('、')} 重校验不再通过`
            : '工位状态已更新，流程重校验无影响',
        ),
    },
  );

  // 「以资产型号为准」：清掉工位上早先登记、与资产不一致的型号（设备方法本来就按资产型号匹配）
  const adoptAssetModel = useMutation(
    (station: StationRow) =>
      api.patch<{ broken_recipes: string[] }>(`/stations/${station.id}`, { model: '', row_version: station.row_version }),
    {
      invalidates: ['stations', 'recipes', 'audit'],
      onSuccess: (result) =>
        toast.push(
          result.broken_recipes.length
            ? `已清除工位上的旧型号；${result.broken_recipes.join('、')} 重校验不再通过`
            : '已清除工位上的旧型号，以资产登记的型号为准',
        ),
    },
  );

  const capabilityName = (id: string) => capabilities.data?.find((row) => row.id === id)?.name ?? id.replace('cap.', '');

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>工位配置</h1>
          <div className="small muted">
            能力极限是流程校验与排程匹配的唯一数据源，修改需要电子签名并触发流程重校验。型号、校准、资产状态从关联的
            <Link to="/assets">仪器设备</Link>带出；清洗确认、指令核查、重连在<Link to="/floor">现场监控</Link>；能力定义在
            <Link to="/capabilities">能力字典</Link>。
          </div>
        </div>
        <div className="row">
          {can('station.edit') ? (
            <button className="btn primary" onClick={() => setAddingStation(true)}>
              登记新工位
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
              <th>关联资产</th>
              <th title="设备方法按这个型号匹配工位">型号</th>
              <th>校准（按资产）</th>
              <th className="num" title="同一时刻能同时承接几个批次的设备步骤">通道</th>
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
                    {station.name} · 岛 {station.island}
                  </div>
                </td>
                <td>
                  {station.retired ? (
                    <Pill state="retired" label="已停用" />
                  ) : (
                    <Pill state={station.status} label={stationLabel(station.status)} />
                  )}
                  <div className={`tiny ${station.clean ? 'muted' : 'warn-text'}`}>
                    {station.clean ? '已清洗' : `待清洗${station.dirty_batch_id ? `（${station.dirty_batch_id} 用后）` : ''}`}
                  </div>
                </td>
                <td className="small">
                  {station.asset ? (
                    <>
                      <Link to="/assets" className="mono">{station.asset.asset_no}</Link>
                      <div className="tiny muted">{station.asset.name}</div>
                      {station.asset.state !== 'active' ? (
                        <div className="tiny warn-text">资产{ASSET_STATE[station.asset.state] ?? station.asset.state}</div>
                      ) : null}
                    </>
                  ) : (
                    <span className="muted" title="设备步骤要落在关联了资产的工位上，开跑检查才能核对校准与容量">未关联</span>
                  )}
                </td>
                <td className="small">
                  <span className="mono">{station.model || '—'}</span>
                  {station.model_source === 'asset' && !station.model ? (
                    <div className="tiny warn-text">资产未登记型号</div>
                  ) : null}
                  {station.model_conflict ? (
                    <div className="tiny warn-text" title="设备方法按资产登记的型号匹配工位；若工位上的才对，请到「仪器设备」改资产型号">
                      工位原登记 <span className="mono">{station.model_conflict}</span>，与资产不一致
                      {can('station.edit') && station.model ? (
                        <div>
                          <button
                            className="btn sm"
                            disabled={adoptAssetModel.pending}
                            onClick={() => adoptAssetModel.run(station).catch((error) => toast.push(error.message))}
                          >
                            以资产型号为准
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </td>
                <td className="small">
                  <AssetCalibration asset={station.asset} />
                </td>
                <td className="num">
                  {station.channels ?? 1}
                  {station.channel_unit === 'sample' ? <div className="tiny muted">按样本</div> : null}
                </td>
                <td className="small">
                  {Object.keys(station.limits).map((capability) => (
                    <span key={capability} className="tag" title={capability}>
                      {capabilityName(capability)}
                    </span>
                  ))}
                </td>
                <td className="row-end">
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
        <div className="panel-body small muted">
          通道按「一个批次的一个设备步骤占 1 个」计，与批次里有几个样本无关：设备若一颗电芯占一个物理通道，
          一批 8 颗的 8 通道柜只能同时跑 1 批，这里就填 1。工位通道数不能超过所属资产容量；资产容量由映射到它的所有工位共用。
        </div>
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
                const [pill, label] = ADAPTER_STATUS[adapter.status] ?? ['fault', adapter.status];
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
                      <div className="tiny muted">v{adapter.version} · 配置 v{adapter.config_version}</div>
                      {adapter.template ? (
                        <div className={`tiny ${adapter.template.outdated ? 'warn-text' : 'muted'}`}>
                          模板 {adapter.template.code} r{adapter.template.revision}
                          {adapter.template.outdated ? `（已发布 r${adapter.template.latest_revision}）` : ''}
                        </div>
                      ) : null}
                      {adapter.catalog?.described_at ? (
                        <div className="tiny muted">
                          {adapter.catalog.vendor || '—'} · 固件 {adapter.catalog.firmware || '—'} · 程序 {adapter.catalog.methods.length} 个
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <Pill state={pill} label={label} />
                      {adapter.acceptance?.required ? (
                        <div className="tiny warn-text" title={adapter.acceptance.reason}>待接入验收（{adapter.acceptance.required_label}）</div>
                      ) : null}
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
                    </td>
                  </tr>
                );
              })}
          </tbody>
        </table>
        <div className="panel-body small muted">
          “模拟器”只验证系统流程，不代表对应协议已经接入真实设备。心跳超过 5 s 标记降级，超过 5 min
          判为心跳超时、挡住用到这台设备的批次。配置里可以套用<Link to="/device-templates">设备接入模板</Link>、测试连接、读取设备方法目录、
          跑接入验收；改了配置的真实设备先「待接入验收」，执行器自动跑只读级，通过了才重新接指令。
          失联后的重连在「现场监控」该工位卡片上做——重连只是重新握手并对账最近检查点，在途批次要不要续跑仍由恢复评估决定。
        </div>
      </Panel>

      {editing ? (
        <LimitsEditor station={editing} capabilities={capabilities.data ?? []} onClose={() => setEditing(null)} />
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
    </div>
  );
}

/** 校准只登记在资产上：这里显示资产按校准记录算出的结论，不在工位上另存一份到期日。 */
function AssetCalibration({ asset }: { asset: StationAsset | null }) {
  if (!asset) return <span className="muted">—</span>;
  if (!asset.calibration_applicable) {
    return (
      <span className="tag" title={asset.calibration_exempt_reason}>
        不适用校准
      </span>
    );
  }
  return (
    <>
      <Pill state={asset.calibration_valid ? 'valid' : 'expired'} label={asset.calibration_valid ? '有效' : '缺失或过期'} />
      {asset.calibration_due ? <div className="tiny muted">至 {day(asset.calibration_due)}</div> : null}
    </>
  );
}

// 驱动与各自的配置项、起步模板来自后端驱动目录（GET /drivers），不写死在页面里；登记新工位后在「适配器配置」里套用模板或手工配置
const DRIVER_HINT = '填已登记的驱动键（如 http_json_v1、line_command_v1）；各驱动的配置项说明与起步模板在「适配器配置」里，保存前按驱动检查配置';

/* 结构化极限编辑。一行一个参数，只填上下限两个数；未列出的参数视为该工位不能承接。
   区间是流程校验与排程匹配的唯一判据，所以这里改完要签名，并当场把受影响的流程列出来。 */
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
            : '已保存并重新校验全部引用流程',
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
        工位未定义的参数视为不能承接该步骤。保存后服务端重校验所有引用这些能力的流程，
        已发布但不再通过的版本进入「需修订」，不能再创建批次。能力本身有哪些参数在「能力字典」里定义。
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

/* 登记新工位。能力极限一并写入并立刻重校验受影响的流程，所以要签名。 */
function StationForm({ capabilities, onClose }: { capabilities: CapabilityRow[]; onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  const [form, setForm] = useState<{
    id: string; name: string; island: number; model: string; channels: number; channel_unit: ChannelUnit;
  }>({ id: 'ST-', name: '', island: 1, model: '', channels: 1, channel_unit: 'batch' });
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
        有资产档案的设备，型号、序列号、校准在「仪器设备」登记，登记完到资产详情里关联本工位。
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
      <div className="grid cols-2">
        <Field label="功能岛">
          <NumberInput value={form.island} ariaLabel="功能岛" onChange={(v) => setForm({ ...form, island: Number(v) || 0 })} />
        </Field>
        <Field label="并行通道数" hint={CHANNELS_HINT}>
          <NumberInput value={form.channels} ariaLabel="并行通道数" invalid={!(form.channels >= 1)} onChange={(v) => setForm({ ...form, channels: Number(v) || 1 })} />
        </Field>
        <Field label="通道计法" hint={CHANNEL_UNIT_HINT}>
          <ChannelUnitSelect value={form.channel_unit} onChange={(value) => setForm({ ...form, channel_unit: value })} />
        </Field>
      </div>
      <div className="grid cols-2">
        <Field label="型号" hint="只给没有资产档案的工位填（如 AGV、机械臂）；关联资产后以资产登记的型号为准">
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
            <Field label="驱动键" hint={DRIVER_HINT}>
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

/* 台账信息不影响排程判据，所以不要签名；能力极限走另一条要签名的路。
   关联了资产的工位不在这里改型号：型号以资产登记为准，设备方法按它匹配。 */
function StationLedgerForm({ station, onClose }: { station: StationRow; onClose: () => void }) {
  const toast = useToast();
  const linked = Boolean(station.asset);
  const [form, setForm] = useState<{
    name: string; model: string; island: number; channels: number; channel_unit: ChannelUnit;
  }>({
    name: station.name, model: station.model, island: station.island, channels: station.channels ?? 1,
    channel_unit: station.channel_unit ?? 'batch',
  });
  const [error, setError] = useState('');

  const save = useMutation(
    (payload: Record<string, unknown>) => api.patch<{ broken_recipes: string[] }>(`/stations/${station.id}`, payload),
    {
      invalidates: ['stations', 'recipes', 'dashboard', 'schedule', 'audit'],
      onSuccess: (result) => {
        toast.push(
          result.broken_recipes?.length
            ? `工位台账已更新；${result.broken_recipes.join('、')} 重校验不再通过`
            : '工位台账已更新',
        );
        onClose();
      },
    },
  );

  const submit = () => {
    const { model, ...rest } = form;
    save
      .run({ ...rest, ...(linked ? {} : { model }), row_version: station.row_version })
      .catch((caught) => setError(caught.message));
  };

  return (
    <Modal
      title={`编辑台账 · ${station.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={save.pending} onClick={submit}>
            保存
          </button>
        </>
      }
    >
      <div className="note">
        这里只改台账信息。能力极限是排程与流程校验的判据，改动要签名并触发重校验，请用「编辑极限」；
        校准、资产状态与总容量在「仪器设备」维护。
      </div>
      <Field label="名称">
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </Field>
      <div className="grid cols-2">
        {linked ? (
          <Field label="型号" hint={`以资产 ${station.asset!.asset_no} 登记的为准，设备方法按它匹配；要改请到「仪器设备」`}>
            <input value={station.model || '（资产未登记型号）'} readOnly />
          </Field>
        ) : (
          <Field label="型号" hint="没有资产档案的工位（如 AGV、机械臂）才在这里填">
            <input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} />
          </Field>
        )}
        <Field label="功能岛">
          <NumberInput value={form.island} ariaLabel="功能岛" onChange={(v) => setForm({ ...form, island: Number(v) || 0 })} />
        </Field>
      </div>
      <div className="grid cols-2">
        <Field label="并行通道数" hint={CHANNELS_HINT}>
          <NumberInput value={form.channels} ariaLabel="并行通道数" invalid={!(form.channels >= 1)} onChange={(v) => setForm({ ...form, channels: Number(v) || 1 })} />
        </Field>
        <Field label="通道计法" hint={CHANNEL_UNIT_HINT}>
          <ChannelUnitSelect value={form.channel_unit} onChange={(value) => setForm({ ...form, channel_unit: value })} />
        </Field>
      </div>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

function stationLabel(status: string): string {
  return { idle: '空闲', running: '运行', fault: '故障', offline: '离线' }[status] ?? status;
}

/** 驱动自报的设备身份与方法目录。空目录不据此筛工位；「*」表示接受任意设备端程序。 */
