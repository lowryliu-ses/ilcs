import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { ApiError, api, pageQuery } from '../../shared/api';
import { day, time } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import { CHANNEL_UNIT_LABEL } from '../../shared/types';
import type { AssetRow, CapabilityRow, ChannelUnit, IslandRow, Paged, StationAsset, StationRow } from '../../shared/types';
import { Blocked, ConfirmDialog, ConnectionPill, Field, Modal, NumberInput, Panel, Pill, useToast } from '../../shared/ui';
import { AdapterEditor } from './AdapterEditor';
import { AreasTab, areaLabel } from './AreasTab';
import { DeviceTemplatesTab } from './DeviceTemplatesTab';
import { LocationsTab } from './LocationsTab';

/* 工位与接入：系统里的执行位置（工位）和它怎么连设备（设备连接），加上同一类设备共用的接入模板。三个页签各有地址：

   - 工位：能接什么活（能力极限）、同时接几份（通道）。实物属性——型号、序列号、校准、资产状态与总容量——归「仪器设备」，
     这里只读显示关联资产的结论；
   - 设备连接：每个工位的适配器（驱动、连接参数、凭据引用、接入验收），还没接设备的工位也列在这里；
   - 接入模板：一类设备怎么接，按修订号发布；工位的设备连接套用它，只填自己的连接参数；
   - 放置位：载具在现场能放在哪（工位放置位、板库槽位、缓冲位、库房），现场布局的静态登记。

   登记新工位只登记台账与关联的仪器设备，之后分两步接设备、填能力极限，各走各的签名与检查。
   清洗确认、结果未知指令转人工核查、适配器重连是现场操作，在「现场监控」做；能力本身的定义在「能力字典」。 */

export type StationsTab = 'ledger' | 'connections' | 'templates' | 'locations' | 'areas';

const TABS: { key: StationsTab; path: string; label: string; perm?: string[] }[] = [
  { key: 'ledger', path: '/stations', label: '工位' },
  { key: 'connections', path: '/stations/connections', label: '设备连接' },
  { key: 'templates', path: '/stations/templates', label: '接入模板', perm: ['station.edit', 'template.release'] },
  { key: 'locations', path: '/stations/locations', label: '载具放置位' },
  { key: 'areas', path: '/stations/areas', label: '实验区' },
];

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

const ASSET_STATE: Record<string, string> = { active: '正常', maintenance: '维护中', retired: '已退役' };

export function StationsPage({ tab = 'ledger' }: { tab?: StationsTab }) {
  const { can } = useSession();
  const [search, setSearch] = useSearchParams();
  const stations = useQuery<StationRow[]>('stations', () => api.get<StationRow[]>('/stations'), 15000);
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const [editing, setEditing] = useState<StationRow | null>(null);
  const [adding, setAdding] = useState<{ assetId: string } | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [editingStation, setEditingStation] = useState<StationRow | null>(null);
  const [editingAdapter, setEditingAdapter] = useState<StationRow | null>(null);
  const [deleting, setDeleting] = useState<StationRow | null>(null);
  const editable = can('station.edit');

  // 从「仪器设备 → 接入系统」过来：/stations?new=1&asset=<资产 ID>，直接打开登记表单并选好这台资产
  useEffect(() => {
    if (search.get('new') === null) return;
    if (editable) setAdding({ assetId: search.get('asset') ?? '' });
    setSearch({}, { replace: true });
  }, [search, setSearch, editable]);

  const tabs = TABS.filter((item) => !item.perm || item.perm.some((perm) => can(perm)));
  const current = tabs.some((item) => item.key === tab) ? tab : 'ledger';
  const rows = stations.data ?? [];
  const unconnected = rows.filter((station) => !station.adapter && !station.retired).length;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>工位与接入</h1>
          <div className="small muted">
            {current === 'ledger' ? (
              <>
                能力极限是流程校验与排程匹配的唯一数据源，修改需要电子签名并触发流程重校验。型号、校准、资产状态从关联的
                <Link to="/assets">仪器设备</Link>带出；清洗确认、指令核查、重连在<Link to="/floor">现场监控</Link>；能力定义在
                <Link to="/capabilities">能力字典</Link>。
              </>
            ) : current === 'connections' ? (
              '每个工位怎么连设备：驱动、连接参数、凭据引用与接入验收。同型号的几台设备套用同一份接入模板，只填各自的连接参数。'
            ) : current === 'locations' ? (
              '载具（板、托盘、架子）在现场能放的位置：工位放置位、板库槽位、缓冲位、库房；样本在载具的孔位里。登记了位置才启用载具位置追踪，转运按位置规划。'
            ) : current === 'areas' ? (
              '工位按所在区域分组（如物料准备段、配液段、测试段）。登记工位时填实验区编号，这里给编号起名字；看板与现场监控按名称显示，改名不影响排程与执行。'
            ) : (
              '一类设备怎么接，存成有版本、要发布的模板；工位在「设备连接」里套用模板、只填自己的连接参数。设备模块交付的 profile.json 在这里导入。'
            )}
          </div>
        </div>
      </div>

      <nav className="seg page-tabs" aria-label="工位与接入">
        {tabs.map((item) => (
          <Link key={item.key} to={item.path} className={item.key === current ? 'on' : ''}>
            {item.label}
            {item.key === 'connections' && unconnected ? <span className="tiny warn-text">（{unconnected} 台未接入）</span> : null}
          </Link>
        ))}
      </nav>

      {current === 'ledger' ? (
        <LedgerPanel
          stations={rows}
          capabilities={capabilities.data ?? []}
          onAdd={editable ? () => setAdding({ assetId: '' }) : undefined}
          onEditLedger={setEditingStation}
          onEditLimits={setEditing}
          onConnect={setEditingAdapter}
          onDelete={setDeleting}
        />
      ) : null}
      {current === 'connections' ? <ConnectionsPanel stations={rows} onConfigure={setEditingAdapter} /> : null}
      {current === 'templates' ? <DeviceTemplatesTab /> : null}
      {current === 'locations' ? <LocationsTab stations={rows} /> : null}
      {current === 'areas' ? <AreasTab /> : null}

      {editing ? (
        <LimitsEditor station={editing} capabilities={capabilities.data ?? []} onClose={() => setEditing(null)} />
      ) : null}
      {adding ? (
        <StationForm
          initialAssetId={adding.assetId}
          onCreated={(id) => {
            setAdding(null);
            setCreated(id);
          }}
          onClose={() => setAdding(null)}
        />
      ) : null}
      {created && !editing && !editingAdapter ? (
        <NextSteps
          stationId={created}
          station={rows.find((row) => row.id === created)}
          onConnect={setEditingAdapter}
          onLimits={setEditing}
          onClose={() => setCreated(null)}
        />
      ) : null}
      {editingStation ? (
        <StationLedgerForm station={editingStation} onClose={() => setEditingStation(null)} />
      ) : null}
      {editingAdapter ? (
        <AdapterEditor station={editingAdapter} onClose={() => setEditingAdapter(null)} />
      ) : null}
      {deleting ? <DeleteStationDialog station={deleting} onClose={() => setDeleting(null)} /> : null}
    </div>
  );
}

function LedgerPanel({
  stations, capabilities, onAdd, onEditLedger, onEditLimits, onConnect, onDelete,
}: {
  stations: StationRow[];
  capabilities: CapabilityRow[];
  onAdd?: () => void;
  onEditLedger: (station: StationRow) => void;
  onEditLimits: (station: StationRow) => void;
  onConnect: (station: StationRow) => void;
  onDelete: (station: StationRow) => void;
}) {
  const { can } = useSession();
  const toast = useToast();
  const areas = useQuery<IslandRow[]>('islands', () => api.get<IslandRow[]>('/islands'));
  const areaName = (id: number) => areas.data?.find((row) => row.id === id)?.name ?? '';
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

  const capabilityName = (id: string) => capabilities.find((row) => row.id === id)?.name ?? id.replace('cap.', '');

  return (
    <Panel
      title="工位台账"
      aside={onAdd ? <button className="btn primary sm" onClick={onAdd}>登记新工位</button> : null}
      flush
    >
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
            <th>设备连接</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {stations.map((station) => {
            return (
              <tr key={station.id} className={station.retired ? 'retired-row' : undefined}>
                <td>
                  <b className="mono">{station.id}</b>
                  <div className="tiny muted">
                    {station.name} · {areaLabel(station.island, areaName(station.island))}
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
                  {Object.keys(station.limits).length ? null : <span className="warn-text">未登记，排程匹配不到</span>}
                </td>
                <td className="small">
                  {station.adapter ? (
                    <Link to="/stations/connections" title="到「设备连接」查看或修改">
                      <ConnectionPill status={station.adapter.status} />
                    </Link>
                  ) : station.retired ? (
                    <span className="muted">未接入</span>
                  ) : can('station.edit') ? (
                    <button className="btn sm" onClick={() => onConnect(station)}>接入设备</button>
                  ) : (
                    <ConnectionPill status={null} />
                  )}
                  {station.adapter?.acceptance?.required ? <div className="tiny warn-text">待接入验收</div> : null}
                </td>
                <td className="row-end">
                  {can('station.edit') ? (
                    <button className="btn sm" onClick={() => onEditLedger(station)}>
                      编辑台账
                    </button>
                  ) : null}
                  {can('station.edit') ? (
                    <button className="btn sm" onClick={() => onEditLimits(station)}>
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
                  {can('station.edit') && station.retired ? (
                    <button className="btn sm" title="登记错了、从没用过的工位可以删除" onClick={() => onDelete(station)}>
                      删除
                    </button>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="panel-body small muted">
        通道按「一个批次的一个设备步骤占 1 个」计，与批次里有几个样本无关：设备若一颗电芯占一个物理通道，
        一批 8 颗的 8 通道柜只能同时跑 1 批，这里就填 1。工位通道数不能超过所属资产容量；资产容量由映射到它的所有工位共用。
      </div>
    </Panel>
  );
}

function ConnectionsPanel({ stations, onConfigure }: { stations: StationRow[]; onConfigure: (station: StationRow) => void }) {
  const { can } = useSession();
  return (
    <Panel title="设备连接" flush>
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
          {stations
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
                    <ConnectionPill status={adapter.status} />
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
                      <button className="btn sm" onClick={() => onConfigure(station)}>
                        连接配置
                      </button>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          {stations
            .filter((station) => !station.adapter && !station.retired)
            .map((station) => (
              <tr key={station.id}>
                <td className="mono">{station.id}</td>
                <td colSpan={6} className="small">
                  <Pill state="neutral" label="未接入" />
                  <span className="muted"> {station.name}：登记时没有接设备，接入前不接设备指令</span>
                </td>
                <td className="row-end">
                  {can('station.edit') ? (
                    <button className="btn sm primary" onClick={() => onConfigure(station)}>
                      接入设备
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
        </tbody>
      </table>
      <div className="panel-body small muted">
        “模拟器”只验证系统流程，不代表对应协议已经接入真实设备。心跳超过 5 s 标记降级，超过 5 min
        判为心跳超时、挡住用到这台设备的批次。连接配置里可以套用<Link to="/stations/templates">接入模板</Link>、测试连接、
        读取设备自报信息、跑接入验收；改了配置的真实设备先「待接入验收」，执行器自动跑只读级，通过了才重新接指令。
        失联后的重连在「现场监控」该工位卡片上做——重连只是重新握手并对账最近检查点，在途批次要不要续跑仍由恢复评估决定。
      </div>
    </Panel>
  );
}

/* 登记完一个工位，还差两步才能接活：接设备（设备连接）、填能力极限。每一步做完回到这里打勾，也可以稍后在列表里补。 */
function NextSteps({
  stationId, station, onConnect, onLimits, onClose,
}: {
  stationId: string;
  station?: StationRow;
  onConnect: (station: StationRow) => void;
  onLimits: (station: StationRow) => void;
  onClose: () => void;
}) {
  const connected = Boolean(station?.adapter);
  const capabilityCount = Object.keys(station?.limits ?? {}).length;
  const done = connected && capabilityCount > 0;
  return (
    <Modal
      title={`已登记 ${stationId}`}
      onClose={onClose}
      footer={<button className={`btn${done ? ' primary' : ''}`} onClick={onClose}>{done ? '完成' : '稍后再说'}</button>}
    >
      {!station ? <div className="muted">正在刷新工位列表…</div> : null}
      <div className="note">工位登记好了。还差下面两步才能接活，也可以稍后在工位列表里补：</div>
      <table className="compact">
        <tbody>
          <tr>
            <td className="small"><b>1. 接入设备</b>：套接入模板或手工配置驱动；保存后执行器握手，真实设备先过接入验收</td>
            <td className="row-end">
              {connected ? (
                <span className="small">已接入：{station?.adapter?.protocol}{station?.adapter?.acceptance?.required ? '（待接入验收）' : ''}</span>
              ) : (
                <button className="btn sm primary" disabled={!station} onClick={() => station && onConnect(station)}>接入设备</button>
              )}
            </td>
          </tr>
          <tr>
            <td className="small"><b>2. 能力极限</b>：这台工位能做哪些能力、参数范围多少；没登记能力的工位排程匹配不到</td>
            <td className="row-end">
              {capabilityCount ? (
                <span className="small">已登记 {capabilityCount} 项能力</span>
              ) : (
                <button className="btn sm primary" disabled={!station} onClick={() => station && onLimits(station)}>填能力极限</button>
              )}
            </td>
          </tr>
        </tbody>
      </table>
      {station && !station.asset ? (
        <div className="small muted">
          没有关联仪器设备：型号、校准与容量按资产计，有资产档案的设备到<Link to="/assets">仪器设备</Link>详情里关联本工位。
        </div>
      ) : null}
    </Modal>
  );
}

/* 删掉登记错了、从没用过的工位（连同它的设备连接）。只对已停用的开放；排过工步、发过指令、做过接入验收的
   只能保持停用——为什么不能删由服务端逐条列出。 */
function DeleteStationDialog({ station, onClose }: { station: StationRow; onClose: () => void }) {
  const toast = useToast();
  const check = useQuery<{ blockers: string[] }>(
    `stations:delete-blockers:${station.id}`, () => api.get<{ blockers: string[] }>(`/stations/${station.id}/delete-blockers`),
  );
  const remove = useMutation(() => api.remove<{ broken_recipes: string[] }>(`/stations/${station.id}`), {
    invalidates: ['stations', 'assets', 'recipes', 'schedule', 'dashboard', 'audit'],
    onSuccess: (result) => {
      toast.push(result.broken_recipes.length
        ? `已删除 ${station.id}；${result.broken_recipes.join('、')} 重校验不再通过`
        : `已删除 ${station.id}`);
      onClose();
    },
  });
  const blockers = check.data?.blockers ?? [];
  if (!check.data) {
    return (
      <Modal title={`删除工位 · ${station.id}`} onClose={onClose}>
        <div className={check.error ? 'note bad' : 'muted'}>{check.error?.message ?? '正在检查有没有记录引用它…'}</div>
      </Modal>
    );
  }
  if (blockers.length) {
    return (
      <Modal title={`删除工位 · ${station.id}`} onClose={onClose} footer={<button className="btn" onClick={onClose}>关闭</button>}>
        <div className="note warn">
          {station.id} 进过历史记录，不能删除，保持停用即可：
          <Blocked reasons={blockers} />
        </div>
      </Modal>
    );
  }
  return (
    <ConfirmDialog
      title={`删除工位 · ${station.id}`}
      danger
      confirmLabel="删除"
      pending={remove.pending}
      error={remove.error?.message}
      onConfirm={() => remove.run().catch(() => undefined)}
      onClose={onClose}
    >
      {station.id}（{station.name}）没有排过工步、发过指令，也没有接入验收记录，可以删除
      {station.adapter ? '，连同它的设备连接' : ''}。删除后这个标识可以重新登记。
    </ConfirmDialog>
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

/* 结构化极限编辑。一行一个参数，只填上下限两个数；未列出的参数视为该工位不能承接。
   区间是流程校验与排程匹配的唯一判据，所以这里改完要签名，并当场把受影响的流程列出来。
   这台工位不再承接的能力整项移除；排好程、还要在这台工位上用它的批次没结束时，服务端会拒绝并列出批次。 */
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
  const [removed, setRemoved] = useState<string[]>([]);
  const [error, setError] = useState('');

  const save = useMutation(
    (payload: { limits: unknown; remove: string[]; signature_id: string }) =>
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
    setRemoved((current) => current.filter((id) => id !== capabilityId));
    setAdding('');
  };

  const removeCapability = (capabilityId: string) => {
    setLimits((current) => Object.fromEntries(Object.entries(current).filter(([id]) => id !== capabilityId)));
    // 原来就登记在工位上的才要让服务端移除；刚在这里加上又删掉的只是撤回
    if (capabilityId in (station.limits ?? {})) setRemoved((current) => [...current, capabilityId]);
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
    await save.run({ limits, remove: removed, signature_id: signatureId }).catch((caught) =>
      setError([caught.message, ...(caught instanceof ApiError ? caught.blocked.map((row) => row.label) : [])].join('；')),
    );
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
            <div className="subsection-head small" style={{ marginBottom: 6 }}>
              <span>
                <b>{definition?.name ?? capabilityId}</b> <span className="tiny muted mono">{capabilityId}</span>
              </span>
              <button
                type="button"
                className="btn sm"
                title="这台工位不再承接这项能力：保存后引用它的流程重校验"
                onClick={() => removeCapability(capabilityId)}
              >
                移除
              </button>
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

      {removed.length ? (
        <div className="note warn">
          将移除 {removed.map((id) => capabilities.find((row) => row.id === id)?.name ?? id).join('、')}：保存后这台工位不再承接它们，
          引用它们的流程随之重校验——没有别的工位能做的会进入「需修订」。
        </div>
      ) : null}

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

/* 登记新工位：只登记台账（标识、名称、实验区、通道）和关联的仪器设备。怎么连设备、能接什么活在登记之后分两步做
   （设备连接、能力极限），各走各的签名与检查——不在这里另嵌一份不能套模板的简化版连接配置。
   关联了资产的工位型号以资产为准；AGV、机械臂这类没有资产档案的，才在这里填型号。 */
function StationForm({
  initialAssetId, onCreated, onClose,
}: {
  initialAssetId: string;
  onCreated: (stationId: string) => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const { sign } = useSignature();
  const [keyword, setKeyword] = useState('');
  const query = pageQuery({ page: 1, page_size: 100, keyword });
  const assets = useQuery<Paged<AssetRow>>(`assets:picker:${query}`, () => api.get<Paged<AssetRow>>(`/assets${query}`));
  const preset = useQuery<AssetRow>(initialAssetId ? `assets:${initialAssetId}` : null, () => api.get<AssetRow>(`/assets/${initialAssetId}`));
  const [form, setForm] = useState<{
    id: string; name: string; island: number; model: string; channels: number; channel_unit: ChannelUnit; asset_id: string;
  }>({ id: 'ST-', name: '', island: 1, model: '', channels: 1, channel_unit: 'batch', asset_id: initialAssetId });
  const [error, setError] = useState('');

  const options = [
    ...(preset.data && !(assets.data?.items ?? []).some((row) => row.id === preset.data!.id) ? [preset.data] : []),
    ...(assets.data?.items ?? []),
  ].filter((row) => row.state !== 'retired' || row.id === form.asset_id);
  const asset = options.find((row) => row.id === form.asset_id);

  // 从资产过来（「接入系统」）：名称缺省用资产名，免得再敲一遍
  useEffect(() => {
    if (preset.data) setForm((current) => (current.name ? current : { ...current, name: preset.data!.name }));
  }, [preset.data]);

  const create = useMutation((payload: Record<string, unknown>) => api.post<{ id: string }>('/stations', payload), {
    invalidates: ['stations', 'assets', 'recipes', 'schedule', 'dashboard', 'audit'],
    onSuccess: (result) => {
      toast.push(`工位 ${result.id} 已登记`);
      onCreated(result.id);
    },
  });

  const ready = /^[A-Za-z0-9-]{3,}$/.test(form.id) && form.name.trim().length > 0;
  const overCapacity = asset ? form.channels > asset.capacity : false;

  const submit = async () => {
    if (!ready) {
      setError('标识至少 3 位（字母、数字、连字符），名称不能为空');
      return;
    }
    if (overCapacity) {
      setError(`并行通道数不能超过资产 ${asset!.asset_no} 的容量 ${asset!.capacity}：先到仪器设备调大容量，或减少通道数`);
      return;
    }
    setError('');
    const signatureId = await sign('登记新工位', form.id, ['工程变更批准']);
    if (!signatureId) return;
    const { model, ...rest } = form;
    await create
      .run({ ...rest, name: form.name.trim(), model: form.asset_id ? '' : model.trim(), limits: {}, signature_id: signatureId })
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
        工位是排程匹配的落点。这里只登记台账；登记之后接着做两步：接入设备（设备连接）、填能力极限。
        有资产档案的设备先在<Link to="/assets">仪器设备</Link>登记实物（型号、序列号、校准），在这里选上它。
      </div>
      <div className="grid cols-2">
        <Field label="标识" hint="例如 ST-08，登记后不可更改">
          <input className="mono" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} />
        </Field>
        <Field label="名称">
          <input value={form.name} placeholder="例如：超声分散站" onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
      </div>
      <Field
        label="关联仪器设备"
        hint={asset
          ? `型号 ${asset.model || '（资产未登记）'} · 容量 ${asset.capacity}；容量、校准许可与型号随资产共享`
          : '不关联：AGV、机械臂这类没有资产档案的工位；设备步骤要落在关联了资产的工位上，开跑检查才能核对校准'}
      >
        <div className="row">
          <input placeholder="资产号、名称或序列号" value={keyword} onChange={(e) => setKeyword(e.target.value)} />
          <select value={form.asset_id} onChange={(e) => setForm({ ...form, asset_id: e.target.value })}>
            <option value="">不关联</option>
            {options.map((row) => (
              <option key={row.id} value={row.id}>
                {row.asset_no} · {row.name}
                {row.model ? ` · ${row.model}` : ''}
                {row.station_ids.length ? `（已映射 ${row.station_ids.join('、')}）` : ''}
              </option>
            ))}
          </select>
        </div>
      </Field>
      <div className="grid cols-2">
        <Field label="实验区" hint="编号；名称在「实验区」页签里起">
          <NumberInput value={form.island} ariaLabel="实验区" onChange={(v) => setForm({ ...form, island: Number(v) || 0 })} />
        </Field>
        {asset ? (
          <Field label="型号" hint={`以资产 ${asset.asset_no} 登记的为准，设备方法按它匹配；要改请到「仪器设备」`}>
            <input value={asset.model || '（资产未登记型号）'} readOnly />
          </Field>
        ) : (
          <Field label="型号" hint="只给没有资产档案的工位填（如 AGV、机械臂）；设备方法按型号匹配工位">
            <input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} />
          </Field>
        )}
      </div>
      <div className="grid cols-2">
        <Field label="并行通道数" hint={CHANNELS_HINT}>
          <NumberInput value={form.channels} ariaLabel="并行通道数" invalid={!(form.channels >= 1) || overCapacity} onChange={(v) => setForm({ ...form, channels: Number(v) || 1 })} />
        </Field>
        <Field label="通道计法" hint={CHANNEL_UNIT_HINT}>
          <ChannelUnitSelect value={form.channel_unit} onChange={(value) => setForm({ ...form, channel_unit: value })} />
        </Field>
      </div>
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
        <Field label="实验区" hint="编号；名称在「实验区」页签里起">
          <NumberInput value={form.island} ariaLabel="实验区" onChange={(v) => setForm({ ...form, island: Number(v) || 0 })} />
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
