/* 点位读写：SiLA 设备服务实现了 PointAccess（驱动宿主上配了点表的设备）就能用，不要求参与自动流程。
   读点在 API 里按点表逐个读（只读、不动设备）；手动写只对声明了可写的点，签名、写明原因，由执行器先读、写、再回读，
   前后值与签名留痕。任务用的控制信号（启动、状态、复位、指令号）不能手动写：要让设备动作请走指令。 */
import { useEffect, useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import type { AdapterRow, PointReading, PointsListing, PointWriteRow, StationRow } from '../../shared/types';
import { NumberInput, Pill, useToast } from '../../shared/ui';

const WRITE_PILL: Record<PointWriteRow['state'], string> = {
  queued: 'scheduled', running: 'running', done: 'done', failed: 'fault', unknown: 'unknown', cancelled: 'cancelled',
};

function shown(value: PointReading['value'], unit = ''): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return `${Number.isInteger(value) ? value : Number(value.toPrecision(7))}${unit ? ` ${unit}` : ''}`;
  return `${value}${unit ? ` ${unit}` : ''}`;
}

function range(point: PointReading): string {
  if (point.min === null && point.max === null) return '';
  return `${point.min ?? '—'}–${point.max ?? '—'}`;
}

/** 写入框：布尔点给选择，数值点（读到的是数或限定了范围）给数字框，其余是文字 */
function ValueInput({ point, value, onChange }: {
  point: PointReading; value: unknown; onChange: (value: number | boolean | string | '') => void;
}) {
  if (typeof point.value === 'boolean') {
    return (
      <select value={value === true ? 'true' : value === false ? 'false' : ''} onChange={(event) => onChange(event.target.value === 'true')}>
        <option value="" disabled>选择</option>
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  }
  if (typeof point.value === 'number' || point.min !== null || point.max !== null) {
    return <NumberInput value={typeof value === 'number' ? value : ''} onChange={onChange} ariaLabel={`${point.name} 写入值`} />;
  }
  return <input value={typeof value === 'string' ? value : ''} onChange={(event) => onChange(event.target.value)} />;
}

export function PointsPanel({ station, adapter }: { station: StationRow; adapter: AdapterRow }) {
  const toast = useToast();
  const { sign } = useSignature();
  const { can } = useSession();
  const writesKey = `stations:point-writes:${station.id}`;
  const [listing, setListing] = useState<PointsListing | null>(null);
  const [readError, setReadError] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState<number | boolean | string | ''>('');
  const [reason, setReason] = useState('');
  const [polling, setPolling] = useState(false);
  const writes = useQuery<PointWriteRow[]>(
    writesKey, () => api.get<PointWriteRow[]>(`/stations/${station.id}/adapter/point-writes?limit=10`), polling ? 2000 : 0,
  );
  const pending = (writes.data ?? []).some((row) => row.state === 'queued' || row.state === 'running');
  useEffect(() => setPolling(pending), [pending]);

  const read = useMutation(() => api.get<PointsListing>(`/stations/${station.id}/adapter/points`), {
    onSuccess: (result) => { setListing(result); setReadError(''); },
  });
  const write = useMutation(
    ({ point, payload }: { point: string; payload: Record<string, unknown> }) =>
      api.post<PointWriteRow>(`/stations/${station.id}/adapter/points/${encodeURIComponent(point)}/write`, payload),
    {
      invalidates: [writesKey, 'audit'],
      onSuccess: (row) => {
        toast.push(`已排队：执行器马上写 ${row.point} = ${shown(row.value)}（先读、写、再回读）`);
        setEditing(null);
        setValue('');
        setReason('');
      },
    },
  );
  const cancel = useMutation((id: string) => api.post<PointWriteRow>(`/point-writes/${id}/cancel`), {
    invalidates: [writesKey, 'audit'], onSuccess: () => toast.push('已撤回排队中的写入'),
  });
  // 写入出了结论之后重新读一遍点位，界面上的值跟着变
  const latest = writes.data?.[0];
  useEffect(() => {
    if (listing && latest && ['done', 'unknown'].includes(latest.state)) {
      read.run().catch(() => undefined);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latest?.id, latest?.state]);

  const submit = async (point: PointReading) => {
    if (value === '' || (typeof value === 'string' && !value.trim())) {
      toast.push('先填要写的值');
      return;
    }
    if (reason.trim().length < 2) {
      toast.push('写明原因（会和前后值一起留痕）');
      return;
    }
    const signatureId = await sign(`写入 ${point.name} = ${shown(value as PointReading['value'])}`, station.id, ['手动写入设备点位']);
    if (!signatureId) return;
    await write.run({ point: point.name, payload: { value, reason: reason.trim(), signature_id: signatureId } })
      .catch(() => undefined);
  };

  const tasks = listing?.tasks ?? adapter.tasks ?? true;
  // 设备服务的驱动配置变了还没批准：点表可能已经把点指到了别的地址，服务端也会拒绝
  const blocked = adapter.driver_awaiting_approval ? '设备服务的驱动配置变了、还没签名批准：批准之后再写' : pending ? '上一条写入还没出结论' : '';
  return (
    <div className="subsection">
      <div className="subsection-head">
        <strong>点位</strong>
        <span className="small muted">
          {tasks
            ? '这台设备也参与自动流程：任务用的控制信号（启动、状态、复位、指令号）不能手动写'
            : '只读写点位：不参与自动流程（排到这里的指令会被拒绝）；要参与自动流程，在连接配置里加能力映射与状态'}
        </span>
        <button className="btn small" disabled={read.pending} onClick={() => read.run().catch((caught) => setReadError(caught instanceof Error ? caught.message : String(caught)))}>
          {read.pending ? '读取中…' : listing ? '重新读取' : '读取点位'}
        </button>
      </div>
      {readError ? <div className="note bad">{readError}</div> : null}
      {listing ? (
        <table className="table compact">
          <thead>
            <tr><th>点</th><th>值</th><th>可写</th><th>{listing.read_at ? `读于 ${clock(listing.read_at)}` : ''}</th></tr>
          </thead>
          <tbody>
            {listing.points.map((point) => (
              <tr key={point.name}>
                <td>
                  <span className="mono">{point.name}</span>
                  {point.label ? <span className="small muted"> · {point.label}</span> : null}
                </td>
                <td>{point.error ? <span className="small bad">{point.error}</span> : <span className="mono">{shown(point.value, point.unit)}</span>}</td>
                <td className="small">
                  {point.writable ? `可写${range(point) ? `（${range(point)}${point.unit ? ` ${point.unit}` : ''}）` : ''}` : point.control ? '控制信号' : '只读'}
                </td>
                <td>
                  {point.writable && can('device.write') ? (
                    editing === point.name ? (
                      <div className="row">
                        <ValueInput point={point} value={value} onChange={setValue} />
                        <input placeholder="原因（必填，会留痕）" value={reason} onChange={(event) => setReason(event.target.value)} />
                        <button className="btn small primary" disabled={write.pending} onClick={() => submit(point)}>签名并写入</button>
                        <button className="btn small" onClick={() => setEditing(null)}>取消</button>
                      </div>
                    ) : (
                      <button className="btn small" disabled={Boolean(blocked)} title={blocked}
                        onClick={() => { setEditing(point.name); setValue(typeof point.value === 'boolean' || typeof point.value === 'number' ? point.value : ''); }}>
                        写入…
                      </button>
                    )
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="small muted">按点表逐个读（只读，不动设备）；接入验收正在执行时不读。</div>
      )}
      {write.error ? <div className="note bad">{write.error.message}</div> : null}
      {(writes.data ?? []).length ? (
        <table className="table compact">
          <thead><tr><th>手动写入</th><th>结论</th><th>前 → 后</th><th>申请</th><th /></tr></thead>
          <tbody>
            {(writes.data ?? []).map((row) => (
              <tr key={row.id}>
                <td><span className="mono">{row.point} = {shown(row.value)}</span><div className="small muted">{row.reason}</div></td>
                <td>
                  <Pill state={WRITE_PILL[row.state] ?? row.state} label={row.state_label} />
                  {row.error ? <div className={`small ${row.state === 'done' ? 'muted' : 'bad'}`}>{row.error}</div> : null}
                </td>
                <td className="mono small">{row.state === 'done' ? `${shown(row.before)} → ${shown(row.after)}` : '—'}</td>
                <td className="small">{row.requested_by} · {clock(row.created_at)}</td>
                <td>
                  {row.state === 'queued' && can('device.write') ? (
                    <button className="btn small" disabled={cancel.pending} onClick={() => cancel.run(row.id).catch(() => undefined)}>撤回</button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}
