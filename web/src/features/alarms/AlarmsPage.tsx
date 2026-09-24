import { useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import type { AlarmRow } from '../../shared/types';
import { Empty, Field, Modal, Panel, Pill, Severity, useToast } from '../../shared/ui';

export function AlarmsPage() {
  const { can } = useSession();
  const toast = useToast();
  const alarms = useQuery<AlarmRow[]>('alarms', () => api.get<AlarmRow[]>('/alarms'), 10000);
  const [shelving, setShelving] = useState<AlarmRow | null>(null);
  const [clearing, setClearing] = useState<AlarmRow | null>(null);

  const invalidates = ['alarms', 'dashboard', 'batches'];
  const act = useMutation((payload: { id: string; action: string }) => api.post(`/alarms/${payload.id}/${payload.action}`), {
    invalidates,
    onSuccess: () => toast.push('报警状态已更新'),
  });

  const rows = alarms.data ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>报警中心</h1>
        <span className="small muted">
          「确认」表示人员已知晓，异常状态仍保留；「关闭」要求设备侧条件已恢复。批次恢复评估另行进行。
        </span>
      </div>

      <Panel title={`报警（未确认 ${rows.filter((alarm) => alarm.state === 'active').length}）`} flush>
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>级别</th>
                <th>报警</th>
                <th>来源</th>
                <th>状态</th>
                <th>处置建议</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((alarm) => (
                <tr key={alarm.id}>
                  <td>
                    <Severity level={alarm.severity} /> {alarm.severity_label}
                  </td>
                  <td>
                    <span className="mono">{alarm.id}</span>
                    <div className="small">
                      {alarm.category_label ? <span className="tag">{alarm.category_label}</span> : null} {alarm.message}
                    </div>
                    <div className="tiny muted">{clock(alarm.raised_at)} · 责任 {alarm.owner || '未指派'}</div>
                  </td>
                  <td className="small mono">
                    {alarm.source_type}:{alarm.source_id}
                  </td>
                  <td>
                    <Pill state={alarm.state} label={alarm.state_label} />
                    {alarm.condition_active ? (
                      <div className="tiny bad-text">
                        {alarm.origin === 'system' ? '软件判定的异常条件仍持续' : '设备侧条件仍持续'}
                      </div>
                    ) : (
                      <div className="tiny muted">条件已恢复</div>
                    )}
                    {alarm.shelved_until ? <div className="tiny muted">搁置至 {alarm.shelved_until}</div> : null}
                  </td>
                  <td className="small muted">{alarm.response || '—'}</td>
                  <td className="row-end">
                    {alarm.state === 'active' && can('alarm.ack') ? (
                      <button
                        className="btn sm"
                        onClick={() => act.run({ id: alarm.id, action: 'ack' }).catch((error) => toast.push(error.message))}
                      >
                        确认
                      </button>
                    ) : null}
                    {alarm.state !== 'closed' && can('alarm.shelve') ? (
                      <button className="btn sm" onClick={() => setShelving(alarm)}>
                        搁置
                      </button>
                    ) : null}
                    {alarm.origin === 'system' && alarm.condition_active && (can('alarm.close') || can('batch.recover')) ? (
                      <button className="btn sm" onClick={() => setClearing(alarm)}>
                        清除条件
                      </button>
                    ) : null}
                    {alarm.state !== 'closed' && can('alarm.close') ? (
                      <button
                        className="btn sm"
                        disabled={alarm.condition_active}
                        title={alarm.condition_active ? '设备侧条件未恢复，不能关闭' : undefined}
                        onClick={() => act.run({ id: alarm.id, action: 'close' }).catch((error) => toast.push(error.message))}
                      >
                        关闭
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>当前没有报警</Empty>
        )}
      </Panel>

      {shelving ? <ShelveDialog alarm={shelving} onClose={() => setShelving(null)} invalidates={invalidates} /> : null}
      {clearing ? <ClearDialog alarm={clearing} onClose={() => setClearing(null)} invalidates={invalidates} /> : null}
    </div>
  );
}

function ShelveDialog({
  alarm,
  onClose,
  invalidates,
}: {
  alarm: AlarmRow;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const [until, setUntil] = useState('');
  const shelve = useMutation(() => api.post(`/alarms/${alarm.id}/shelve`, { until }), {
    invalidates,
    onSuccess: () => {
      toast.push(`${alarm.id} 已搁置至 ${until}`);
      onClose();
    },
  });

  return (
    <Modal
      title={`搁置 · ${alarm.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={!until || shelve.pending} onClick={() => shelve.run().catch(() => undefined)}>
            搁置
          </button>
        </>
      }
    >
      <div className="note warn">搁置必须设定到期时间，到期后重新计入未确认报警。</div>
      <Field label="搁置至">
        <input type="date" value={until} onChange={(event) => setUntil(event.target.value)} />
      </Field>
      {shelve.error ? <div className="note bad">{shelve.error.message}</div> : null}
    </Modal>
  );
}

/* 软件判定的报警设备不知道它的存在，不可能上报恢复：由人写明原因、签名后清除条件。 */
function ClearDialog({
  alarm,
  onClose,
  invalidates,
}: {
  alarm: AlarmRow;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const { sign } = useSignature();
  const [reason, setReason] = useState('');
  const clear = useMutation(
    (signatureId: string) =>
      api.post(`/alarms/${alarm.id}/clear-condition`, { reason, signature_id: signatureId }),
    {
      invalidates,
      onSuccess: () => {
        toast.push(`${alarm.id} 异常条件已清除`);
        onClose();
      },
    },
  );
  const submit = async () => {
    const signatureId = await sign('清除报警条件', alarm.id, ['异常原因已消除']);
    if (!signatureId) return;
    await clear.run(signatureId).catch(() => undefined);
  };

  return (
    <Modal
      title={`清除条件 · ${alarm.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={!reason.trim() || clear.pending} onClick={submit}>
            签名并清除
          </button>
        </>
      }
    >
      <div className="note warn">{alarm.message}</div>
      <Field label="原因已如何消除（必填）">
        <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {clear.error ? <div className="note bad">{clear.error.message}</div> : null}
    </Modal>
  );
}
