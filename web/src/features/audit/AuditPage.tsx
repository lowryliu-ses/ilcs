import { useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useQuery } from '../../shared/query';
import type { AuditRow } from '../../shared/types';
import { Empty, Panel, Pill } from '../../shared/ui';

export function AuditPage() {
  const [target, setTarget] = useState('');
  const events = useQuery<AuditRow[]>(
    `audit:${target}`,
    () => api.get<AuditRow[]>(target ? `/audit?target=${encodeURIComponent(target)}` : '/audit?limit=200'),
    15000,
  );

  const rows = events.data ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>审计日志</h1>
        <div className="row">
          <input
            placeholder="按对象过滤，例如 B-260920-001"
            value={target}
            onChange={(event) => setTarget(event.target.value.trim())}
          />
          <span className="small muted">仅追加；设备事件也可作为操作者</span>
        </div>
      </div>

      <Panel title={`事件（${rows.length}）`} flush>
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>操作者</th>
                <th>动作</th>
                <th>对象</th>
                <th>前 → 后</th>
                <th>说明</th>
                <th>电子签名</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((event) => (
                <tr key={event.id ?? `${event.time}-${event.action}`}>
                  <td className="small mono">{clock(event.time)}</td>
                  <td>
                    {event.user}
                    <div className="tiny muted">{event.role}</div>
                  </td>
                  <td>{event.action}</td>
                  <td className="mono small">{event.target}</td>
                  <td className="small">
                    {event.before || '—'} → {event.after || '—'}
                  </td>
                  <td className="small muted">
                    {event.detail || '—'}
                    {event.command_id ? <div className="tiny mono">指令 {event.command_id.slice(0, 8)}</div> : null}
                    {event.checkpoint_id ? <div className="tiny mono">检查点 {event.checkpoint_id.slice(0, 8)}</div> : null}
                  </td>
                  <td>
                    {event.sign ? (
                      <Pill state="running" label={`已签名 · ${event.meaning}`} />
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>没有匹配的审计事件</Empty>
        )}
      </Panel>
    </div>
  );
}
