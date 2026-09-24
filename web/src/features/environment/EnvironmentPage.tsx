/* 环境监测：区域（工位编号或房间 / 手套箱名）× 指标的最新读数。
   步骤声明的环境要求按这里的最新读数核对：没有读数、超过有效期没更新、超出范围，开跑检查与设备投递都不放行。
   读数由传感器经服务身份上报（POST /api/runtime/environment），也可以人工抄录。 */
import { useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { EnvironmentReadingRow } from '../../shared/types';
import { Field, ListState, Panel, Pill, useToast } from '../../shared/ui';

const METRICS: [string, string, string][] = [
  ['temperature', '温度', '℃'], ['humidity', '相对湿度', '%RH'], ['dew_point', '露点', '℃'], ['h2o_ppm', '水含量', 'ppm'],
  ['o2_ppm', '氧含量', 'ppm'], ['pressure_diff', '压差', 'Pa'], ['particles', '洁净度', '个/m³'],
];

export function EnvironmentPage() {
  const toast = useToast();
  const { can } = useSession();
  const readings = useQuery<EnvironmentReadingRow[]>('environment', () => api.get<EnvironmentReadingRow[]>('/environment/readings'), 30000);
  const [form, setForm] = useState({ zone: '', metric: 'humidity', value: '', note: '' });
  const record = useMutation(
    () => api.post('/environment/readings', { zone: form.zone.trim(), metric: form.metric, value: Number(form.value), note: form.note }),
    {
      invalidates: ['environment'],
      onSuccess: () => {
        toast.push('读数已记录');
        setForm({ ...form, value: '', note: '' });
      },
    },
  );
  return (
    <div className="page">
      <div className="page-head">
        <h1>环境监测</h1>
        <span className="small muted">步骤的环境要求按这里的最新读数核对；超过有效期没更新的读数不作为放行依据。</span>
      </div>
      {can('environment.record') ? (
        <Panel title="人工抄录">
          <div className="filters">
            <Field label="区域">
              <input value={form.zone} placeholder="如 GB-01、干燥间" onChange={(event) => setForm({ ...form, zone: event.target.value })} />
            </Field>
            <Field label="指标">
              <select value={form.metric} onChange={(event) => setForm({ ...form, metric: event.target.value })}>
                {METRICS.map(([value, label, unit]) => (
                  <option key={value} value={value}>
                    {label}（{unit}）
                  </option>
                ))}
              </select>
            </Field>
            <Field label="读数">
              <input type="number" value={form.value} onChange={(event) => setForm({ ...form, value: event.target.value })} />
            </Field>
            <Field label="备注">
              <input value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} />
            </Field>
            <button
              className="btn primary"
              disabled={!form.zone.trim() || form.value === '' || record.pending}
              onClick={() => record.run().catch((error) => toast.push(error.message))}
            >
              记录
            </button>
          </div>
        </Panel>
      ) : null}
      <Panel title="最新读数" flush>
        <ListState loading={readings.loading && !readings.data} error={readings.error} empty={!readings.data?.length} emptyText="还没有环境读数" />
        {readings.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>区域</th>
                <th>指标</th>
                <th>读数</th>
                <th>时间</th>
                <th>来源</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {readings.data.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.zone}</td>
                  <td>{row.metric_label}</td>
                  <td className="mono">
                    {row.value} {row.unit}
                  </td>
                  <td className="small">
                    {clock(row.measured_at)}
                    <div className="tiny muted">{row.age_min} min 前</div>
                  </td>
                  <td className="small">
                    {row.source === 'device' ? '传感器' : '人工'} · {row.recorded_by}
                  </td>
                  <td>
                    <Pill state={row.stale ? 'fault' : 'running'} label={row.stale ? '已过期' : '有效'} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>
    </div>
  );
}
