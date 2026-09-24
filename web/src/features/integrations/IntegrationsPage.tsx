/* 集成与事件：出向事件（Webhook）订阅。

   业务事件在业务事务里写进发件箱，执行器在事务外投递：2xx 算送达，其余指数退避重试，超过次数判死信。
   每次投递带 X-ILCS-Signature（HMAC-SHA256，密钥只在新建 / 轮换时显示一次）与 X-ILCS-Event-Id，
   接收方按事件编号去重。载荷只有「发生了什么」与定位字段，详情由接收方带服务凭据回来取。
   入向事件（外部系统唤醒业务事件等待节点）走 POST /api/runtime/batches/{id}/signals。 */
import { useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import type { WebhookDeliveryRow, WebhookRow } from '../../shared/types';
import { Empty, Field, ListState, Modal, Panel, Pill, useToast } from '../../shared/ui';

type Topic = { topic: string; label: string };

export function IntegrationsPage() {
  const toast = useToast();
  const hooks = useQuery<WebhookRow[]>('webhooks', () => api.get<WebhookRow[]>('/webhooks'), 20000);
  const topics = useQuery<Topic[]>('webhooks:topics', () => api.get<Topic[]>('/webhooks/topics'));
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<{ name: string; secret: string } | null>(null);
  const [viewing, setViewing] = useState<WebhookRow | null>(null);
  const toggle = useMutation(
    (row: WebhookRow) => api.patch(`/webhooks/${row.id}`, { enabled: !row.enabled, row_version: row.row_version }),
    { invalidates: ['webhooks'], onSuccess: () => toast.push('已更新订阅') },
  );
  const rotate = useMutation((row: WebhookRow) => api.post<WebhookRow>(`/webhooks/${row.id}/rotate`), {
    invalidates: ['webhooks'],
    onSuccess: (row) => setSecret({ name: row.name, secret: row.secret ?? '' }),
  });
  const ping = useMutation((row: WebhookRow) => api.post(`/webhooks/${row.id}/ping`), {
    invalidates: ['webhooks'],
    onSuccess: () => toast.push('已排一条测试事件，执行器下一轮投递'),
  });

  return (
    <div className="page">
      <div className="page-head">
        <h1>集成与事件</h1>
        <button className="btn primary" onClick={() => setCreating(true)}>
          新建订阅
        </button>
      </div>
      <div className="note">
        出向事件：批次 / 步骤状态变化、异常登记与收尾、报警、重排建议、报告发布、业务信号、流程通知节点。
        每次投递带 <span className="mono">X-ILCS-Signature</span>（HMAC-SHA256，对「时间戳.正文」签名）与
        <span className="mono"> X-ILCS-Event-Id</span>，接收方按事件编号去重；只向允许清单里的主机投递，不跟随重定向。
        入向事件由外部系统调用 <span className="mono">POST /api/runtime/batches/&#123;id&#125;/signals</span>，服务身份须在
        <span className="mono"> batch_signals</span> 范围内授权事件名。
      </div>

      <Panel title="订阅" flush>
        <ListState loading={hooks.loading && !hooks.data} error={hooks.error} empty={!hooks.data?.length} emptyText="还没有订阅" />
        {hooks.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>地址</th>
                <th>主题</th>
                <th>最近投递</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {hooks.data.map((row) => (
                <tr key={row.id}>
                  <td>
                    <b>{row.name}</b>
                  </td>
                  <td className="small mono">{row.url}</td>
                  <td className="small">{row.topics.join('、')}</td>
                  <td className="small">
                    {row.last_success_at ? `成功 ${clock(row.last_success_at)}` : '—'}
                    {row.consecutive_failures ? (
                      <div className="tiny bad-text">
                        连续失败 {row.consecutive_failures} 次：{row.last_error}
                      </div>
                    ) : null}
                  </td>
                  <td>
                    <Pill state={row.enabled ? 'running' : 'done'} label={row.enabled ? '启用' : '停用'} />
                  </td>
                  <td className="row-end">
                    <button className="btn sm" onClick={() => setViewing(row)}>
                      投递记录
                    </button>
                    <button className="btn sm" disabled={ping.pending || !row.enabled} onClick={() => ping.run(row).catch((error) => toast.push(error.message))}>
                      测试
                    </button>
                    <button className="btn sm" disabled={rotate.pending} onClick={() => rotate.run(row).catch((error) => toast.push(error.message))}>
                      轮换密钥
                    </button>
                    <button className="btn sm" disabled={toggle.pending} onClick={() => toggle.run(row).catch((error) => toast.push(error.message))}>
                      {row.enabled ? '停用' : '启用'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      {creating ? (
        <CreateDialog
          topics={topics.data ?? []}
          onClose={() => setCreating(false)}
          onCreated={(row) => {
            setCreating(false);
            setSecret({ name: row.name, secret: row.secret ?? '' });
          }}
        />
      ) : null}
      {secret ? (
        <Modal title={`签名密钥 · ${secret.name}`} onClose={() => setSecret(null)}>
          <div className="note warn">这是唯一一次显示密钥。请交给接收方保存，用它校验 X-ILCS-Signature；关掉后只能轮换。</div>
          <input readOnly value={secret.secret} className="mono" style={{ width: '100%' }} onFocus={(event) => event.target.select()} />
        </Modal>
      ) : null}
      {viewing ? <DeliveriesDialog hook={viewing} onClose={() => setViewing(null)} /> : null}
    </div>
  );
}

function CreateDialog({
  topics,
  onClose,
  onCreated,
}: {
  topics: Topic[];
  onClose: () => void;
  onCreated: (row: WebhookRow) => void;
}) {
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [chosen, setChosen] = useState<string[]>(['batch.state_changed', 'exception.opened']);
  const create = useMutation(() => api.post<WebhookRow>('/webhooks', { name, url, topics: chosen }), {
    invalidates: ['webhooks'],
    onSuccess: onCreated,
  });
  return (
    <Modal
      title="新建出向事件订阅"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={create.pending || !name.trim() || !url.trim() || !chosen.length} onClick={() => create.run().catch(() => undefined)}>
            新建
          </button>
        </>
      }
    >
      <Field label="名称">
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="如：LIMS 结果回填" />
      </Field>
      <Field label="接收地址" hint="主机必须在 ILCS_WEBHOOK_ALLOWED_HOSTS 里；正式环境只允许 https">
        <input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://lims.example.internal/ilcs/events" />
      </Field>
      <Field label="订阅主题">
        <div className="dep-list">
          {topics.map((row) => (
            <label key={row.topic} className="check">
              <input
                type="checkbox"
                checked={chosen.includes(row.topic)}
                onChange={(event) => setChosen(event.target.checked ? [...chosen, row.topic] : chosen.filter((value) => value !== row.topic))}
              />
              {row.label} <span className="tiny muted mono">{row.topic}</span>
            </label>
          ))}
        </div>
      </Field>
      {create.error ? <div className="note bad">{create.error.message}</div> : null}
    </Modal>
  );
}

function DeliveriesDialog({ hook, onClose }: { hook: WebhookRow; onClose: () => void }) {
  const toast = useToast();
  const deliveries = useQuery<WebhookDeliveryRow[]>(`webhooks:${hook.id}:deliveries`, () =>
    api.get<WebhookDeliveryRow[]>(`/webhooks/${hook.id}/deliveries`), 10000,
  );
  const redeliver = useMutation((id: string) => api.post(`/webhook-deliveries/${id}/redeliver`), {
    invalidates: [`webhooks:${hook.id}:deliveries`],
    onSuccess: () => toast.push('已重新排队；事件编号不变，接收方按它去重'),
  });
  return (
    <Modal title={`投递记录 · ${hook.name}`} wide onClose={onClose}>
      {deliveries.data?.length ? (
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>主题</th>
              <th>状态</th>
              <th>结果</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {deliveries.data.map((row) => (
              <tr key={row.id}>
                <td className="small mono">{clock(row.created_at)}</td>
                <td className="small mono">
                  {row.topic}
                  <div className="tiny muted">{row.event_id}</div>
                </td>
                <td>
                  <Pill state={{ delivered: 'running', pending: 'scheduled', dead: 'fault' }[row.state] ?? 'neutral'} label={row.state} />
                  <div className="tiny muted">第 {row.attempts} 次</div>
                </td>
                <td className="small">
                  {row.response_status ? `HTTP ${row.response_status}` : ''} {row.last_error}
                  {row.state === 'pending' && row.next_attempt_at ? <div className="tiny muted">下次 {clock(row.next_attempt_at)}</div> : null}
                </td>
                <td className="row-end">
                  {row.state !== 'pending' ? (
                    <button className="btn sm" disabled={redeliver.pending} onClick={() => redeliver.run(row.id).catch((error) => toast.push(error.message))}>
                      重投
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty>还没有投递</Empty>
      )}
    </Modal>
  );
}
