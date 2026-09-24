import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useLive, useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { QrLabel } from '../../shared/labels';
import type { Floor, FloorSlot, FloorStation, LabwareRow, LabwareType, LocationRow } from '../../shared/types';
import { Blocked, Empty, Field, Modal, Panel, Pill, useToast } from '../../shared/ui';

/* 现场监控：中控室大屏看的就是这一页。

   一眼要能回答三件事：哪台设备在干什么（或为什么不能干）、每块板在哪、有什么在路上。
   数据由服务端变更推送驱动刷新；推送断开时退回 10 s 轮询，顶栏的「实时 / 轮询」标明当前模式。 */

const COMMAND_LABEL: Record<string, string> = {
  dispatch: '动作', resume: '续跑', retry: '重试', hold: '保持', abort: '终止', transfer: '转运',
};
const COMMAND_STATE_LABEL: Record<string, string> = {
  sent: '排队', accepted: '已接受', running: '执行中', unknown: '结果未知', manual: '人工核查',
};

function stationHealth(station: FloorStation): { tone: 'ok' | 'warn' | 'bad' | 'off'; label: string } {
  if (station.retired) return { tone: 'off', label: '已退役' };
  const adapter = station.adapter;
  if (!adapter) return { tone: 'off', label: '无适配器' };
  if (!adapter.enabled) return { tone: 'off', label: '适配器停用' };
  if (!adapter.connected) return { tone: 'bad', label: '失联' };
  if (adapter.interlock) return { tone: 'bad', label: '安全联锁' };
  if (station.status === 'fault') return { tone: 'bad', label: '故障' };
  if (!adapter.accepts_commands) return { tone: 'warn', label: '拒收动作' };
  if (adapter.heartbeat_age_sec !== null && adapter.heartbeat_age_sec > 30) {
    return { tone: 'warn', label: `心跳 ${adapter.heartbeat_age_sec} s 前` };
  }
  const acting = station.commands.some((c) => c.motion && ['accepted', 'running'].includes(c.state));
  return { tone: 'ok', label: acting ? '执行中' : '空闲' };
}

function Slot({ slot }: { slot: FloorSlot }) {
  const labware = slot.labware;
  const cls = !slot.active ? 'off' : labware ? (labware.state === 'other' ? 'other' : 'full') : slot.incoming ? 'incoming' : 'empty';
  const title = [
    slot.name,
    labware ? `载具 ${labware.barcode}${labware.batch_id ? ` · 批次 ${labware.batch_id}` : ''}` : '空',
    slot.incoming ? `转运在途：${slot.incoming.barcode || '（他组织）'} 正送往此处` : '',
    slot.active ? '' : '已停用',
  ].filter(Boolean).join('\n');
  return (
    <div className={`slot ${cls}`} title={title}>
      <span className="slot-id">{slot.id.split('/').pop()}</span>
      <span className="slot-body">
        {labware ? labware.barcode : slot.incoming ? `→ ${slot.incoming.barcode || '在途'}` : '—'}
      </span>
      {labware?.batch_id ? (
        <Link className="slot-batch" to={`/batches/${labware.batch_id}`}>
          {labware.batch_id}
        </Link>
      ) : null}
    </div>
  );
}

function StationCard({ station }: { station: FloorStation }) {
  const health = stationHealth(station);
  const active = station.commands.filter((c) => c.state !== 'sent');
  const queued = station.commands.filter((c) => c.state === 'sent').length;
  return (
    <div className={`station-card ${health.tone}`}>
      <div className="station-head">
        <div>
          <b>{station.name}</b>
          <div className="tiny muted mono">
            {station.id}
            {station.channels > 1 ? ` · ${station.channels} 通道` : ''}
          </div>
        </div>
        <span className={`health ${health.tone}`}>{health.label}</span>
      </div>
      <div className="station-work">
        {active.length ? (
          active.map((command) => (
            <div key={command.id} className="small">
              <Pill state={command.state} label={COMMAND_STATE_LABEL[command.state] ?? command.state} />{' '}
              {COMMAND_LABEL[command.type] ?? command.type}
              {command.batch_id ? (
                <>
                  {' · '}
                  <Link to={`/batches/${command.batch_id}`}>{command.batch_id}</Link> 第 {command.step_index + 1} 步
                </>
              ) : null}
              <span className="tiny muted"> · {clock(command.since)} 起</span>
            </div>
          ))
        ) : (
          <span className="tiny muted">无在途指令</span>
        )}
        {queued ? <div className="tiny muted">队列中 {queued} 条</div> : null}
      </div>
      {station.nests.length ? (
        <div className="slots">
          {station.nests.map((slot) => (
            <Slot key={slot.id} slot={slot} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function FloorPage() {
  const { can } = useSession();
  const { live } = useLive();
  const floor = useQuery<Floor>('floor', () => api.get<Floor>('/floor'), 10000);
  const [scanning, setScanning] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const data = floor.data;

  const islands = useMemo(() => {
    const groups = new Map<number, FloorStation[]>();
    for (const station of data?.stations ?? []) {
      groups.set(station.island, [...(groups.get(station.island) ?? []), station]);
    }
    return [...groups.entries()].sort(([a], [b]) => a - b);
  }, [data]);

  const tally = useMemo(() => {
    const stations = data?.stations ?? [];
    const health = stations.map(stationHealth);
    return {
      busy: health.filter((h) => h.label === '执行中').length,
      idle: health.filter((h) => h.label === '空闲').length,
      bad: health.filter((h) => h.tone === 'bad').length,
      transfers: data?.transfers.length ?? 0,
    };
  }, [data]);

  return (
    <div className="page floor">
      <div className="page-head">
        <h1>现场监控</h1>
        <span className="small muted">
          执行中 <b>{tally.busy}</b> · 空闲 <b>{tally.idle}</b> · 异常 <b className={tally.bad ? 'bad-text' : ''}>{tally.bad}</b> · 在途转运{' '}
          <b>{tally.transfers}</b> · {live ? '实时推送' : '轮询刷新'} · 数据时间 {data ? clock(data.now) : '—'}
        </span>
        <div className="row-end">
          <button className="btn" onClick={() => setInspecting(true)}>
            载具与标签
          </button>
          {can('labware.move') ? (
            <>
              <button className="btn" onClick={() => setRegistering(true)}>
                登记载具
              </button>
              <button className="btn primary" onClick={() => setScanning(true)}>
                扫码放置
              </button>
            </>
          ) : null}
        </div>
      </div>

      {data && !data.tracking ? (
        <div className="note">
          尚未登记放置位与板库：载具位置追踪未启用，转运按排程时间窗处理。管理员可在「工位配置」按现场布局登记位置。
        </div>
      ) : null}
      {data?.lost.length ? (
        <div className="banner bad">
          位置未知的载具（部分执行或转运途中终止）：{data.lost.map((row) => row.barcode).join('、')}。扫码确认实际位置后才能继续使用。
        </div>
      ) : null}

      {islands.map(([island, stations]) => (
        <Panel key={island} title={island ? `岛 #${island}` : '转运与公共'} flush>
          <div className="station-grid">
            {stations.map((station) => (
              <StationCard key={station.id} station={station} />
            ))}
          </div>
        </Panel>
      ))}

      {data?.storage.map((group) => (
        <Panel key={group.group} title={`板库 / 缓冲：${group.group}`} aside={`${group.slots.filter((s) => s.labware).length}/${group.slots.length} 已占用`}>
          <div className="slots wide">
            {group.slots.map((slot) => (
              <Slot key={slot.id} slot={slot} />
            ))}
          </div>
        </Panel>
      ))}

      <Panel title={`在途转运（${data?.transfers.length ?? 0}）`} flush>
        {data?.transfers.length ? (
          <table>
            <thead>
              <tr>
                <th>载具</th>
                <th>从</th>
                <th>到</th>
                <th>承运</th>
                <th>状态</th>
                <th>批次</th>
                <th>开始</th>
              </tr>
            </thead>
            <tbody>
              {data.transfers.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.barcode || '（他组织）'}</td>
                  <td className="mono small">{row.from}</td>
                  <td className="mono small">{row.to}</td>
                  <td className="mono small">{row.carrier}</td>
                  <td>
                    <Pill state={row.state} label={COMMAND_STATE_LABEL[row.state] ?? row.state} />
                  </td>
                  <td>{row.batch_id ? <Link to={`/batches/${row.batch_id}`}>{row.batch_id}</Link> : '—'}</td>
                  <td className="small">{clock(row.since)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>没有在途转运</Empty>
        )}
      </Panel>

      {scanning ? <ScanDialog onClose={() => setScanning(false)} /> : null}
      {registering ? <RegisterDialog onClose={() => setRegistering(false)} /> : null}
      {inspecting ? <LabwareDialog onClose={() => setInspecting(false)} /> : null}
    </div>
  );
}

function freeLocations(locations: LocationRow[] | undefined, keep = ''): LocationRow[] {
  return (locations ?? []).filter((row) => row.active && (!row.occupant || row.id === keep));
}

function ScanDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const [barcode, setBarcode] = useState('');
  const [found, setFound] = useState<LabwareRow | null>(null);
  const [target, setTarget] = useState('');
  const [reason, setReason] = useState('');
  const [error, setError] = useState<ApiError | null>(null);
  const locations = useQuery<LocationRow[]>('locations', () => api.get<LocationRow[]>('/locations'));

  useEffect(() => input.current?.focus(), []);

  const lookup = async () => {
    setError(null);
    const rows = await api.get<LabwareRow[]>(`/labware?keyword=${encodeURIComponent(barcode.trim())}`);
    const exact = rows.find((row) => row.barcode === barcode.trim()) ?? null;
    setFound(exact);
    if (!exact) toast.push('没有这个条码的载具：先登记');
  };

  const move = useMutation(
    () =>
      api.post<LabwareRow>(
        `/labware/${found!.id}/move`,
        { barcode: barcode.trim(), to_location_id: target || null, reason },
        true,
      ),
    { invalidates: ['floor', 'locations', 'labware', 'batches'], onSuccess: () => toast.push('位置已更新') },
  );

  return (
    <Modal
      title="扫码放置载具"
      onClose={onClose}
      footer={
        <button
          className="btn primary"
          disabled={!found || move.pending}
          onClick={() =>
            move
              .run()
              .then(onClose)
              .catch((caught) => setError(caught))
          }
        >
          确认放置
        </button>
      }
    >
      <Field label="载具条码" hint="扫码枪输入后回车">
        <input
          ref={input}
          value={barcode}
          onChange={(event) => {
            setBarcode(event.target.value);
            setFound(null);
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && barcode.trim()) void lookup();
          }}
        />
      </Field>
      {found ? (
        <div className="note">
          {found.type_name} · 当前位置 <b className="mono">{found.location_id || '未上线'}</b>
          {found.state === 'lost' ? '（位置未知，需重新定位）' : ''}
          {found.batch_id ? ` · 批次 ${found.batch_id}` : ''}
          {found.in_transit ? ` · 转运在途到 ${found.in_transit.to}` : ''}
        </div>
      ) : null}
      <Field label="放到" hint="留空表示从产线取下">
        <select value={target} onChange={(event) => setTarget(event.target.value)} disabled={!found}>
          <option value="">（取下 / 未上线）</option>
          {freeLocations(locations.data, found?.location_id).map((row) => (
            <option key={row.id} value={row.id}>
              {row.id} · {row.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="说明">
        <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="例如：人工上料到进样板库" />
      </Field>
      {error ? <Blocked reasons={error.blocked.length ? error.blocked.map((b) => b.label) : [error.message]} /> : null}
    </Modal>
  );
}

function RegisterDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const types = useQuery<LabwareType[]>('labware:types', () => api.get<LabwareType[]>('/labware-types'));
  const locations = useQuery<LocationRow[]>('locations', () => api.get<LocationRow[]>('/locations'));
  const [barcode, setBarcode] = useState('');
  const [typeId, setTypeId] = useState('');
  const [location, setLocation] = useState('');
  const [error, setError] = useState<ApiError | null>(null);
  const create = useMutation(
    () => api.post<LabwareRow>('/labware', { barcode: barcode.trim(), type_id: typeId, location_id: location || null }, true),
    { invalidates: ['floor', 'locations', 'labware'], onSuccess: () => toast.push('载具已登记') },
  );
  return (
    <Modal
      title="登记载具"
      onClose={onClose}
      footer={
        <button
          className="btn primary"
          disabled={!barcode.trim() || !typeId || create.pending}
          onClick={() =>
            create
              .run()
              .then(onClose)
              .catch((caught) => setError(caught))
          }
        >
          登记
        </button>
      }
    >
      <Field label="条码">
        <input value={barcode} onChange={(event) => setBarcode(event.target.value)} autoFocus />
      </Field>
      <Field label="类型">
        <select value={typeId} onChange={(event) => setTypeId(event.target.value)}>
          <option value="">选择</option>
          {(types.data ?? []).filter((row) => row.active).map((row) => (
            <option key={row.id} value={row.id}>
              {row.name}（{row.rows}×{row.cols}）
            </option>
          ))}
        </select>
      </Field>
      <Field label="初始位置" hint="可留空，之后扫码放置">
        <select value={location} onChange={(event) => setLocation(event.target.value)}>
          <option value="">（暂不上线）</option>
          {freeLocations(locations.data).map((row) => (
            <option key={row.id} value={row.id}>
              {row.id} · {row.name}
            </option>
          ))}
        </select>
      </Field>
      {error ? <Blocked reasons={error.blocked.length ? error.blocked.map((b) => b.label) : [error.message]} /> : null}
    </Modal>
  );
}

type LabwareSamples = LabwareRow & {
  samples: { id: string; barcode: string; well: string; sample_type: string; lifecycle_state: string }[];
};

/** 扫一块载具：看它装着哪些样本（按实体孔位）、打印载具标签。 */
function LabwareDialog({ onClose }: { onClose: () => void }) {
  const [code, setCode] = useState('');
  const [found, setFound] = useState<LabwareSamples | null>(null);
  const [error, setError] = useState('');
  const lookup = async () => {
    setError('');
    try {
      setFound(await api.get<LabwareSamples>(`/labware/${encodeURIComponent(code.trim())}/samples`));
    } catch (caught) {
      setFound(null);
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  };
  return (
    <Modal title="载具与标签" wide onClose={onClose}>
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          if (code.trim()) void lookup();
        }}
      >
        <input autoFocus placeholder="扫描或输入载具条码后回车" value={code} onChange={(event) => setCode(event.target.value)} />
        <button className="btn sm" type="submit" disabled={!code.trim()}>
          查找
        </button>
      </form>
      {error ? <div className="note bad">{error}</div> : null}
      {found ? (
        <>
          <div className="small">
            <b className="mono">{found.barcode}</b> · {found.type_name}（{found.rows}×{found.cols}）· 位置 {found.location_id || '未上线'}
            {found.batch_id ? <> · 批次 <Link to={`/batches/${found.batch_id}`}>{found.batch_id}</Link></> : null}
          </div>
          <QrLabel path={`/labware/${found.id}/qr`} cacheKey={`labware:${found.id}:qr`} />
          {found.samples.length ? (
            <table>
              <thead>
                <tr>
                  <th>孔位</th>
                  <th>样本</th>
                  <th>类型</th>
                </tr>
              </thead>
              <tbody>
                {found.samples.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">{row.well}</td>
                    <td>
                      <Link to={`/samples/${row.id}`}>{row.id}</Link>
                      {row.barcode ? <div className="tiny muted">{row.barcode}</div> : null}
                    </td>
                    <td className="small">{row.sample_type}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>载具上没有登记样本</Empty>
          )}
        </>
      ) : null}
    </Modal>
  );
}
