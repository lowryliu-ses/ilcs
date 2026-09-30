/* 载具放置位：载具（板、托盘、架子）在现场能放在哪——工位上的放置位、板库槽位、缓冲位、库房。

   这是现场布局的静态登记，所以放在「工位与接入」；哪个位置上现在放着什么在「现场监控」看。
   登记一个也没有时载具位置追踪不启用，转运按排程时间窗处理。位置编号全站唯一，登记后不改：
   载具移动记录与转运指令都指向它。按工位或板库一次登记多个，编号照种子数据的写法续号
   （工位放置位 `ST-05/N1`，板库槽位 `HOTEL-01/S01`）；不再使用的位置停用，上面有载具或正有转运送过来时不能停用。 */
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { invalidate, useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { LabwareType, LocationRow, StationRow } from '../../shared/types';
import { Field, ListState, Modal, NumberInput, Panel, Pill, useToast } from '../../shared/ui';

const KIND_LABEL: Record<string, string> = { nest: '工位放置位', hotel: '板库槽位', buffer: '缓冲位', storage: '库房' };
// 载具种类（载具类型的 kind）：位置的「可放载具」按它限制，空表示不限
const LABWARE_KIND_LABEL: Record<string, string> = { plate: '孔板', tray: '托盘', rack: '样品架', holder: '夹具' };

type Kind = 'nest' | 'hotel' | 'buffer' | 'storage';

export function LocationsTab({ stations }: { stations: StationRow[] }) {
  const { can } = useSession();
  const toast = useToast();
  const locations = useQuery<LocationRow[]>('locations', () => api.get<LocationRow[]>('/locations'), 30000);
  const [kind, setKind] = useState('');
  const [registering, setRegistering] = useState(false);
  const editable = can('location.edit');

  const toggle = useMutation(
    // 位置编号里带「/」（ST-05/N1）：逐段编码，斜杠照原样留在路径里（接口按 path 取整段编号）
    (row: LocationRow) =>
      api.post<LocationRow>(`/locations/${row.id.split('/').map(encodeURIComponent).join('/')}/active`, { active: !row.active }),
    {
      invalidates: ['locations', 'floor', 'audit'],
      onSuccess: (row) => toast.push(row.active ? `${row.id} 已启用` : `${row.id} 已停用`),
    },
  );

  const rows = (locations.data ?? []).filter((row) => !kind || row.kind === kind);
  const stationName = (id: string) => stations.find((row) => row.id === id)?.name ?? '';

  return (
    <>
      <Panel
        title={`载具放置位（${locations.data?.length ?? 0}）`}
        aside={
          <div className="filters">
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="">全部类型</option>
              {Object.entries(KIND_LABEL).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
            {editable ? <button className="btn primary sm" onClick={() => setRegistering(true)}>登记位置</button> : null}
          </div>
        }
        flush
      >
        <ListState
          loading={locations.loading && !locations.data}
          error={locations.error}
          empty={!rows.length}
          emptyText={locations.data?.length ? '没有这类位置' : '还没有登记载具放置位：载具位置追踪不启用，转运按排程时间窗处理'}
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>编号</th>
                <th>名称</th>
                <th>类型</th>
                <th>所属</th>
                <th className="num">序号</th>
                <th>可放载具</th>
                <th>当前</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={row.active ? undefined : 'retired-row'}>
                  <td className="mono">{row.id}</td>
                  <td>{row.name}</td>
                  <td className="small">{KIND_LABEL[row.kind] ?? row.kind}</td>
                  <td className="small">
                    {row.station_id ? (
                      <>
                        <span className="mono">{row.station_id}</span>
                        <div className="tiny muted">{stationName(row.station_id)}</div>
                      </>
                    ) : null}
                    {row.group && row.group !== row.station_id ? <div className="tiny muted">{row.group}</div> : null}
                  </td>
                  <td className="num">{row.position || '—'}</td>
                  <td className="small">
                    {row.accepts.length ? row.accepts.map((item) => LABWARE_KIND_LABEL[item] ?? item).join('、') : <span className="muted">不限</span>}
                  </td>
                  <td className="small">
                    {row.active ? (
                      row.occupant ? <span title="上面有载具，或正有转运送过来">{row.occupant}</span> : <span className="muted">空</span>
                    ) : (
                      <Pill state="retired" label="已停用" />
                    )}
                  </td>
                  <td className="row-end">
                    {editable ? (
                      <button
                        className={`btn sm${row.active ? '' : ' primary'}`}
                        disabled={toggle.pending || (row.active && !!row.occupant)}
                        title={row.active && row.occupant ? '上面有载具或正有转运送过来，先移走再停用' : row.active ? '停用后不再作为转运目的地' : '重新启用'}
                        onClick={() => toggle.run(row).catch((error) => toast.push(error.message))}
                      >
                        {row.active ? '停用' : '启用'}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <div className="panel-body small muted">
          位置编号全站唯一、登记后不改，载具移动记录与转运指令都指向它；不再使用的位置停用，不删除。
          每个位置上现在放着什么、正在往哪转运，在<Link to="/floor">现场监控</Link>看。
        </div>
      </Panel>

      {registering ? (
        <RegisterDialog stations={stations} existing={locations.data ?? []} onClose={() => setRegistering(false)} />
      ) : null}
    </>
  );
}

/** 同一前缀下已用到的最大序号：`ST-05/N2` → 2，`HOTEL-01/S12` → 12 */
function lastIndex(existing: LocationRow[], prefix: string): number {
  return existing.reduce((max, row) => {
    const matched = row.id.startsWith(`${prefix}/`) ? /(\d+)$/.exec(row.id.slice(prefix.length + 1)) : null;
    return matched ? Math.max(max, Number(matched[1])) : max;
  }, 0);
}

/* 登记位置。工位放置位按工位续号（`ST-05/N3` 起），板库槽位与缓冲位按分组续号（`HOTEL-02/S01` 起），
   一次登记多个；库房是单个位置，编号自己起。登记一个个提交，中途被拒就停下，已登记的保留并说清楚。 */
function RegisterDialog({ stations, existing, onClose }: { stations: StationRow[]; existing: LocationRow[]; onClose: () => void }) {
  const toast = useToast();
  const types = useQuery<LabwareType[]>('labware:types', () => api.get<LabwareType[]>('/labware-types'));
  const [kind, setKind] = useState<Kind>('nest');
  const [stationId, setStationId] = useState('');
  const [group, setGroup] = useState('');
  const [name, setName] = useState('');
  const [storageId, setStorageId] = useState('');
  const [count, setCount] = useState<number | ''>(1);
  const [accepts, setAccepts] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

  const station = stations.find((row) => row.id === stationId);
  const choosable = stations.filter((row) => !row.retired && !(Object.keys(row.limits).length && Object.keys(row.limits).every((cap) => cap === 'cap.transfer')));

  // 要登记的位置：编号、名称、分组、序号都按现有的续上
  const planned = useMemo(() => {
    const total = typeof count === 'number' && count > 0 ? Math.min(count, 96) : 0;
    if (kind === 'storage') {
      const ident = storageId.trim();
      return ident ? [{ id: ident, name: name.trim() || ident, group: group.trim(), position: 0, station_id: '' }] : [];
    }
    if (kind === 'nest') {
      if (!station) return [];
      const start = lastIndex(existing, station.id);
      return Array.from({ length: total }, (_, index) => ({
        id: `${station.id}/N${start + index + 1}`,
        name: `${name.trim() || station.name} 放置位 ${start + index + 1}`,
        group: `实验区 #${station.island}`, position: start + index + 1, station_id: station.id,
      }));
    }
    const prefix = group.trim();
    if (!prefix) return [];
    const start = lastIndex(existing, prefix);
    const label = kind === 'hotel' ? '槽位' : '缓冲位';
    return Array.from({ length: total }, (_, index) => ({
      id: `${prefix}/S${String(start + index + 1).padStart(2, '0')}`,
      name: `${name.trim() || prefix} ${label} ${start + index + 1}`,
      group: prefix, position: start + index + 1, station_id: '',
    }));
  }, [kind, station, group, name, storageId, count, existing]);

  const taken = planned.filter((row) => existing.some((item) => item.id === row.id)).map((row) => row.id);

  const submit = async () => {
    setError('');
    setPending(true);
    const done: string[] = [];
    try {
      for (const row of planned) {
        await api.post('/locations', { ...row, kind, accepts }, true);
        done.push(row.id);
      }
      toast.push(`已登记 ${done.length} 个位置`);
      onClose();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(done.length ? `已登记 ${done.join('、')}；${planned[done.length]?.id} 被拒：${message}` : message);
    } finally {
      setPending(false);
      // 中途被拒时已登记的也要刷新出来
      if (done.length) ['locations', 'floor', 'audit'].forEach(invalidate);
    }
  };

  const kindsInUse = (key: string) => (types.data ?? []).filter((row) => row.kind === key).map((row) => row.name);

  return (
    <Modal
      title="登记位置"
      wide
      onClose={() => {
        if (!pending) onClose();
      }}
      footer={
        <>
          <button className="btn" disabled={pending} onClick={onClose}>取消</button>
          <button className="btn primary" disabled={pending || !planned.length || taken.length > 0} onClick={submit}>
            {pending ? '登记中…' : `登记 ${planned.length} 个位置`}
          </button>
        </>
      }
    >
      <div className="note">
        工位放置位是工位上能放载具的位子（多通道设备每个通道一个）；板库槽位、缓冲位是公共存放架上的格子；库房是单个存放点。
        编号登记后不改，不用的位置停用。
      </div>
      <div className="grid cols-2">
        <Field label="类型">
          <select value={kind} onChange={(event) => setKind(event.target.value as Kind)}>
            {Object.entries(KIND_LABEL).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
        </Field>
        {kind === 'nest' ? (
          <Field label="工位" hint="承运工位（AGV）自己不是放置目标，不列出">
            <select value={stationId} onChange={(event) => setStationId(event.target.value)}>
              <option value="">选择工位</option>
              {choosable.map((row) => (
                <option key={row.id} value={row.id}>{row.id} · {row.name}（{row.channels} 个通道）</option>
              ))}
            </select>
          </Field>
        ) : kind === 'storage' ? (
          <Field label="编号" hint="全站唯一，例如 FRIDGE-02/L2">
            <input className="mono" value={storageId} onChange={(event) => setStorageId(event.target.value)} />
          </Field>
        ) : (
          <Field label={kind === 'hotel' ? '板库编号' : '缓冲架编号'} hint="槽位编号按它续号，例如 HOTEL-02 → HOTEL-02/S01">
            <input className="mono" value={group} placeholder={kind === 'hotel' ? 'HOTEL-02' : 'BUF-01'} onChange={(event) => setGroup(event.target.value)} />
          </Field>
        )}
      </div>
      <div className="grid cols-2">
        <Field label="名称" hint={kind === 'storage' ? '例如：冷藏柜 2 层' : '留空用工位名 / 编号；每个位置名称后面自动加序号'}>
          <input value={name} placeholder={kind === 'hotel' ? '进样板库' : ''} onChange={(event) => setName(event.target.value)} />
        </Field>
        {kind === 'storage' ? (
          <Field label="分组" hint="可选：同一组在现场监控里画在一起">
            <input value={group} onChange={(event) => setGroup(event.target.value)} />
          </Field>
        ) : (
          <Field label="数量" hint={kind === 'nest' && station ? `这台工位有 ${station.channels} 个通道；已登记 ${lastIndex(existing, station.id)} 个放置位` : '一次最多 96 个'}>
            <NumberInput value={count} ariaLabel="数量" invalid={!(typeof count === 'number' && count >= 1 && count <= 96)} onChange={setCount} />
          </Field>
        )}
      </div>
      <Field label="可放载具" hint="不勾表示不限；勾了就只接受这几种载具">
        <div className="row">
          {Object.entries(LABWARE_KIND_LABEL).map(([key, label]) => (
            <label key={key} className="check" title={kindsInUse(key).join('、') || '目前没有这类载具类型'}>
              <input
                type="checkbox"
                checked={accepts.includes(key)}
                onChange={(event) =>
                  setAccepts((current) => (event.target.checked ? [...current, key] : current.filter((item) => item !== key)))
                }
              />
              {label}
            </label>
          ))}
        </div>
      </Field>
      {planned.length ? (
        <div className="small">
          将登记：<span className="mono">{planned.map((row) => row.id).join('、')}</span>
        </div>
      ) : null}
      {taken.length ? <div className="note bad">这些编号已经登记过：{taken.join('、')}</div> : null}
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}
