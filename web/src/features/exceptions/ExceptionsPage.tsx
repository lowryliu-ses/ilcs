/* 异常处理：统一异常事件与策略库。

   报警是「让人知道」，异常事件是「这件事怎么处理」：类别、影响了哪些批次 / 样本 / 工位、
   系统按哪条策略自动做了什么、结果如何、人做了什么、最终怎么恢复。

   策略库只决定「这类异常用什么动作」。会重新驱动设备的动作（重试、改派、跳过）只在指令从未送达
   设备时生效；设备可能已经动过的，服务端一律转人工，策略改不了这一条。安全联锁只能转人工。 */
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { ExceptionEventRow, ExceptionRuleRow, ExceptionSummary } from '../../shared/types';
import { Empty, Field, ListState, Metric, Modal, NumberInput, Panel, Pill, useToast } from '../../shared/ui';

const CATEGORIES: [string, string][] = [
  ['device_fault', '设备故障'],
  ['communication', '通信异常'],
  ['sample', '样本异常'],
  ['reagent', '试剂异常'],
  ['timeout', '超时'],
  ['data', '数据异常'],
  ['robot', '机器人 / 转运异常'],
  ['path_conflict', '位置与路径冲突'],
  ['manual', '人工操作异常'],
  ['safety', '安全异常'],
  ['schedule', '排程冲突'],
  ['system', '系统异常'],
];

const ACTIONS: [ExceptionRuleRow['action'], string][] = [
  ['reroute', '改派到具备同样能力的工位'],
  ['retry', '延时后重新下发（同一工位）'],
  ['skip', '跳过该步骤（流程须标为可跳过）'],
  ['reschedule', '生成重排建议'],
  ['hold', '保持，转人工处理'],
];

const STATE_TONE: Record<string, string> = {
  open: 'fault', manual: 'paused', auto_resolved: 'running', resolved: 'done', closed: 'done',
};

export function ExceptionsPage() {
  const [tab, setTab] = useState<'events' | 'rules'>('events');
  const [state, setState] = useState('open');
  const [category, setCategory] = useState('');
  const [detail, setDetail] = useState<ExceptionEventRow | null>(null);
  const query = `?state=${state}&category=${category}`;
  const events = useQuery<ExceptionEventRow[]>(`exceptions:${query}`, () => api.get<ExceptionEventRow[]>(`/exceptions${query}`), 10000);
  const summary = useQuery<ExceptionSummary>('exceptions:summary', () => api.get<ExceptionSummary>('/exceptions/summary'), 15000);

  return (
    <div className="page">
      <div className="page-head">
        <h1>异常处理</h1>
        <span className="small muted">
          每条异常都记下类别、影响面、系统自动做了什么、人做了什么、最终怎么恢复。自动重试 / 改派只在指令从未送达设备时发生。
        </span>
      </div>

      <div className="metrics">
        <Metric label="待处理" value={summary.data?.open ?? '—'} hint="待处理与处理中" />
        <Metric label="已自动处理" value={summary.data?.auto_resolved ?? '—'} hint="按策略库改派 / 重试 / 跳过成功" />
        <Metric label="累计" value={summary.data?.total ?? '—'} />
        <Metric
          label="待处理类别"
          value={summary.data?.by_category?.[0]?.label ?? '—'}
          hint={(summary.data?.by_category ?? []).map((row) => `${row.label} ${row.count}`).join(' · ') || '无'}
        />
      </div>

      <div className="filters" style={{ margin: '8px 0' }}>
        <button className={`btn sm${tab === 'events' ? ' primary' : ''}`} onClick={() => setTab('events')}>
          异常事件
        </button>
        <button className={`btn sm${tab === 'rules' ? ' primary' : ''}`} onClick={() => setTab('rules')}>
          处理策略库
        </button>
      </div>

      {tab === 'events' ? (
        <Panel
          title="异常事件"
          aside={
            <div className="filters">
              <select value={state} onChange={(event) => setState(event.target.value)}>
                <option value="open">待处理 / 处理中</option>
                <option value="auto_resolved">已自动处理</option>
                <option value="resolved">已恢复</option>
                <option value="closed">已关闭</option>
                <option value="">全部</option>
              </select>
              <select value={category} onChange={(event) => setCategory(event.target.value)}>
                <option value="">全部类别</option>
                {CATEGORIES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
          }
          flush
        >
          <ListState loading={events.loading && !events.data} error={events.error} empty={!events.data?.length} emptyText="没有符合条件的异常" />
          {events.data?.length ? (
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>类别</th>
                  <th>来源</th>
                  <th>影响</th>
                  <th>自动处理</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {events.data.map((row) => (
                  <tr key={row.id} className="clickable" onClick={() => setDetail(row)}>
                    <td className="small mono">{clock(row.created_at)}</td>
                    <td>
                      <span className="tag">{row.category_label}</span>
                      <div className="tiny muted">{row.message.slice(0, 60)}</div>
                    </td>
                    <td className="small mono">
                      {row.batch_id ? (
                        <Link to={`/batches/${row.batch_id}`} onClick={(event) => event.stopPropagation()}>
                          {row.batch_id}
                        </Link>
                      ) : null}
                      {row.step_index >= 0 ? ` · 第 ${row.step_index + 1} 步` : ''}
                      {row.station_id ? <div className="tiny muted">{row.station_id}</div> : null}
                    </td>
                    <td className="small">
                      {row.impact.batches?.length ?? 0} 个批次 · {row.impact.samples ?? 0} 个样本
                    </td>
                    <td className="small">
                      {row.auto_action ? <b>{row.auto_action_label}</b> : <span className="muted">—</span>}
                      <div className="tiny muted">{row.auto_result || row.decision}</div>
                    </td>
                    <td>
                      <Pill state={STATE_TONE[row.state] ?? 'neutral'} label={row.state_label} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </Panel>
      ) : (
        <RulesPanel />
      )}

      {detail ? <EventDialog event={detail} onClose={() => setDetail(null)} /> : null}
    </div>
  );
}

function EventDialog({ event, onClose }: { event: ExceptionEventRow; onClose: () => void }) {
  const { can } = useSession();
  const toast = useToast();
  const [note, setNote] = useState(event.manual_note);
  const handle = useMutation(
    (action: 'claim' | 'resolve' | 'close') => api.post(`/exceptions/${event.id}/handle`, { action, note }),
    {
      invalidates: ['exceptions', 'dashboard'],
      onSuccess: () => {
        toast.push('已记录处理结果');
        onClose();
      },
    },
  );
  const open = ['open', 'manual'].includes(event.state);
  return (
    <Modal
      title={`异常 · ${event.category_label}`}
      wide
      onClose={onClose}
      footer={
        open && can('exception.handle') ? (
          <>
            {event.state === 'open' ? (
              <button className="btn" disabled={handle.pending} onClick={() => handle.run('claim').catch(() => undefined)}>
                认领处理
              </button>
            ) : null}
            <button className="btn" disabled={handle.pending || !note.trim()} onClick={() => handle.run('close').catch(() => undefined)}>
              关闭（无需处理）
            </button>
            <button className="btn primary" disabled={handle.pending || !note.trim()} onClick={() => handle.run('resolve').catch(() => undefined)}>
              标为已恢复
            </button>
          </>
        ) : (
          <button className="btn" onClick={onClose}>
            关闭
          </button>
        )
      }
    >
      <div className={`note${event.state === 'open' ? ' bad' : ''}`}>{event.message}</div>
      <table>
        <tbody>
          <tr>
            <td className="small muted">来源</td>
            <td className="small mono">
              {event.source_type} · {event.source_id}
              {event.command_id ? ` · 指令 ${event.command_id.slice(0, 8)}` : ''}
              {event.never_sent ? <span className="tag">指令未送达设备</span> : null}
            </td>
          </tr>
          <tr>
            <td className="small muted">影响</td>
            <td className="small">
              批次 {(event.impact.batches ?? []).join('、') || '—'} · 样本 {event.impact.samples ?? 0} 个 · 工位{' '}
              {(event.impact.stations ?? []).join('、') || '—'} · 任务 {(event.impact.tasks ?? []).join('、') || '—'}
            </td>
          </tr>
          <tr>
            <td className="small muted">策略判定</td>
            <td className="small">{event.decision || '—'}</td>
          </tr>
          <tr>
            <td className="small muted">自动处理</td>
            <td className="small">
              {event.auto_action ? `${event.auto_action_label}：${event.auto_result}` : '无'}
            </td>
          </tr>
          <tr>
            <td className="small muted">人工处理</td>
            <td className="small">
              {event.manual_by ? `${event.manual_by} · ${event.manual_action} · ${event.manual_note}` : '无'}
            </td>
          </tr>
          <tr>
            <td className="small muted">最终结果</td>
            <td className="small">{event.final_result || '—'}</td>
          </tr>
        </tbody>
      </table>
      {open && can('exception.handle') ? (
        <Field label="处理结果（标为已恢复 / 关闭时必填）">
          <textarea rows={3} value={note} onChange={(changed) => setNote(changed.target.value)} />
        </Field>
      ) : null}
      {handle.error ? <div className="note bad">{handle.error.message}</div> : null}
    </Modal>
  );
}

const BLANK: Omit<ExceptionRuleRow, 'id' | 'category_label' | 'action_label' | 'updated_at' | 'row_version'> = {
  name: '', category: 'communication', match: {}, action: 'reroute', params: { max_attempts: 1 }, priority: 100,
  enabled: true, note: '',
};

function RulesPanel() {
  const { can } = useSession();
  const rules = useQuery<ExceptionRuleRow[]>('exceptions:rules', () => api.get<ExceptionRuleRow[]>('/exception-rules'));
  const [editing, setEditing] = useState<(typeof BLANK & { id?: string; row_version?: number }) | null>(null);
  const editable = can('exception.rules');
  return (
    <Panel
      title="处理策略库"
      aside={
        editable ? (
          <button className="btn sm primary" onClick={() => setEditing({ ...BLANK })}>
            新增策略
          </button>
        ) : (
          <span className="small muted">维护策略需要 exception.rules 权限</span>
        )
      }
      flush
    >
      <div className="panel-body small muted">
        按优先级取第一条匹配的启用策略；没有匹配时转人工。匹配可以按能力、工位、步骤类型、流程、步骤细分，留空表示不限。
        工位失联时没有配策略，也会生成一份重排建议等调度确认，不会自动挪动任何预约。
      </div>
      {rules.data?.length ? (
        <table>
          <thead>
            <tr>
              <th className="num">优先级</th>
              <th>策略</th>
              <th>类别</th>
              <th>匹配</th>
              <th>动作</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {rules.data.map((row) => (
              <tr key={row.id} className={editable ? 'clickable' : ''} onClick={() => editable && setEditing({ ...row })}>
                <td className="num">{row.priority}</td>
                <td>
                  <b>{row.name}</b>
                  {row.note ? <div className="tiny muted">{row.note}</div> : null}
                </td>
                <td>{row.category_label}</td>
                <td className="small mono">
                  {Object.entries(row.match).filter(([, value]) => value).map(([key, value]) => `${key}=${value}`).join(' ') || '不限'}
                </td>
                <td className="small">
                  {row.action_label}
                  {row.params.max_attempts ? <div className="tiny muted">最多 {row.params.max_attempts} 次</div> : null}
                  {row.action === 'retry' ? <div className="tiny muted">间隔 {row.params.delay_sec ?? 60} s</div> : null}
                </td>
                <td>
                  <Pill state={row.enabled ? 'running' : 'done'} label={row.enabled ? '启用' : '停用'} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty>还没有策略：所有异常都转人工处理</Empty>
      )}
      {editing ? <RuleDialog rule={editing} onClose={() => setEditing(null)} /> : null}
    </Panel>
  );
}

function RuleDialog({
  rule,
  onClose,
}: {
  rule: typeof BLANK & { id?: string; row_version?: number };
  onClose: () => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState(rule);
  const save = useMutation(
    () => (draft.id ? api.put(`/exception-rules/${draft.id}`, draft) : api.post('/exception-rules', draft)),
    {
      invalidates: ['exceptions'],
      onSuccess: () => {
        toast.push('策略已保存');
        onClose();
      },
    },
  );
  const set = (change: Partial<typeof draft>) => setDraft({ ...draft, ...change });
  const setMatch = (key: string, value: string) => set({ match: { ...draft.match, [key]: value } });
  const safety = draft.category === 'safety';
  return (
    <Modal
      title={draft.id ? `编辑策略 · ${draft.name}` : '新增策略'}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={save.pending || !draft.name.trim()} onClick={() => save.run().catch(() => undefined)}>
            保存
          </button>
        </>
      }
    >
      <Field label="名称">
        <input value={draft.name} onChange={(event) => set({ name: event.target.value })} placeholder="如：混匀工位失联改派" />
      </Field>
      <div className="grid cols-2">
        <Field label="异常类别">
          <select value={draft.category} onChange={(event) => set({ category: event.target.value, action: event.target.value === 'safety' ? 'hold' : draft.action })}>
            {CATEGORIES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="处理动作" hint={safety ? '安全异常只能转人工' : '重试 / 改派 / 跳过只在指令从未送达设备时生效'}>
          <select value={draft.action} disabled={safety} onChange={(event) => set({ action: event.target.value as ExceptionRuleRow['action'] })}>
            {ACTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {draft.action === 'retry' || draft.action === 'reroute' ? (
        <div className="grid cols-2">
          <Field label="最多自动处理次数" hint="同一批次同一步累计；超过后转人工">
            <NumberInput
              value={draft.params.max_attempts ?? ''}
              ariaLabel="最多次数"
              onChange={(next) => set({ params: { ...draft.params, max_attempts: next === '' ? undefined : next } })}
            />
          </Field>
          {draft.action === 'retry' ? (
            <Field label="重试间隔 s">
              <NumberInput
                value={draft.params.delay_sec ?? 60}
                ariaLabel="重试间隔"
                onChange={(next) => set({ params: { ...draft.params, delay_sec: next === '' ? 0 : next } })}
              />
            </Field>
          ) : null}
        </div>
      ) : null}
      <div className="grid cols-2">
        <Field label="只匹配能力">
          <input value={draft.match.capability ?? ''} placeholder="如 cap.mix；留空不限" onChange={(event) => setMatch('capability', event.target.value)} />
        </Field>
        <Field label="只匹配工位">
          <input value={draft.match.station_id ?? ''} placeholder="如 ST-01-A；留空不限" onChange={(event) => setMatch('station_id', event.target.value)} />
        </Field>
        <Field label="只匹配流程">
          <input value={draft.match.recipe_id ?? ''} placeholder="如 R-205；留空不限" onChange={(event) => setMatch('recipe_id', event.target.value)} />
        </Field>
        <Field label="只匹配步骤">
          <input value={draft.match.step_id ?? ''} placeholder="如 s03；留空不限" onChange={(event) => setMatch('step_id', event.target.value)} />
        </Field>
      </div>
      <div className="grid cols-2">
        <Field label="优先级" hint="数字小的先匹配">
          <NumberInput value={draft.priority} ariaLabel="优先级" onChange={(next) => set({ priority: next === '' ? 100 : next })} />
        </Field>
        <Field label="状态">
          <label className="check">
            <input type="checkbox" checked={draft.enabled} onChange={(event) => set({ enabled: event.target.checked })} />
            启用
          </label>
        </Field>
      </div>
      <Field label="说明">
        <input value={draft.note} onChange={(event) => set({ note: event.target.value })} />
      </Field>
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}
