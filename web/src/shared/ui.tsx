/* 展示层原子组件。状态词汇与配色集中在这里，页面不自己拼颜色。 */
import type { ReactNode } from 'react';
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';

import type { Check, Gate } from './types';

/* 状态词汇与配色的唯一出处。新增状态一律加在这里，页面不自己拼颜色。 */
const STATE_CLASS: Record<string, string> = {
  // 起草、待办
  planned: 'planned', draft: 'planned', pending: 'planned', unassigned: 'planned',
  registered: 'planned', unassessed: 'planned', uploading: 'planned',
  // 已安排、待处理
  scheduled: 'scheduled', review: 'scheduled', locked: 'scheduled', sent: 'scheduled',
  ready: 'scheduled', pending_accept: 'scheduled', collecting: 'scheduled',
  data_review: 'scheduled', reporting: 'scheduled', received: 'scheduled', queued: 'scheduled',
  // 进行中、有效
  running: 'running', released: 'running', approved: 'running', accepted: 'running',
  valid: 'running', published: 'running', in_use: 'running', collected: 'running',
  available: 'running', delivered: 'running', on_duty: 'running',
  // 保持、可疑、需人工判断
  paused: 'paused', held: 'paused', shelved: 'paused', suspect: 'paused', manual: 'paused',
  waiting: 'paused', maybe_sent: 'paused', leave: 'paused', expiring: 'paused',
  stored: 'paused', maintenance: 'paused',
  // 故障、无效、被拒
  fault: 'fault', failed: 'fault', unknown: 'fault', invalid: 'fault', active: 'fault',
  rejected: 'fault', expired: 'fault', revoked: 'fault', unreachable: 'fault',
  missing: 'fault', disabled: 'fault', partial: 'fault',
  // 结束、归档
  done: 'done', retired: 'done', closed: 'done', skipped: 'done', acked: 'done',
  completed: 'done', superseded: 'done', exhausted: 'done', left: 'done', split: 'done',
  // 终止、取消
  aborted: 'aborted', aborting: 'aborted', cancelled: 'aborted', disposed: 'aborted', not_sent: 'aborted',
  not_executed: 'aborted',
  scrapped: 'aborted',
};

/** 步骤类型的中文名。批次详情、任务中心、报告都读同一份。 */
export const STEP_KIND_LABEL: Record<string, string> = {
  device: '设备', manual: '人工', wait: '等待', review: '审核', gate: '质检关卡', split: '样本拆分',
};

/** 开跑检查的三种结论。「不适用」不是通过的近义词，颜色也不一样。 */
export const CHECK_STATE_LABEL: Record<string, string> = {
  pass: '通过', blocked: '阻塞', not_applicable: '不适用',
};

export function Pill({ state, label }: { state: string; label?: string }) {
  return <span className={`pill ${STATE_CLASS[state] ?? 'neutral'}`}>{label ?? state}</span>;
}

export function Panel({
  title,
  aside,
  children,
  flush,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="panel">
      <header>
        <h2>{title}</h2>
        {aside ? <div className="panel-aside">{aside}</div> : null}
      </header>
      <div className={flush ? '' : 'panel-body'}>{children}</div>
    </section>
  );
}

export function Metric({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {hint ? <span className="metric-hint">{hint}</span> : null}
    </div>
  );
}

export function CheckList({ checks }: { checks: Check[] }) {
  return (
    <ul className="check-list">
      {checks.map((check) => {
        const state = check.state ?? (check.ok ? 'pass' : 'blocked');
        const className = state === 'blocked' ? 'bad' : state === 'not_applicable' ? 'na' : 'ok';
        const mark = state === 'blocked' ? '✕' : state === 'not_applicable' ? '—' : '✓';
        return (
          <li key={check.key} className={className}>
            <span className="check-mark">{mark}</span>
            <div>
              <b>{check.label}</b>
              {state === 'not_applicable' ? <span className="tag">不适用</span> : null}
              <div className="small muted">{check.detail}</div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** 列表分页。默认每页 20 条；筛选状态由调用方保留在自己的 state 里。 */
export function Pager({
  page,
  pageSize,
  total,
  onChange,
}: {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return <div className="pager tiny muted">共 {total} 条</div>;
  return (
    <div className="pager">
      <span className="tiny muted">
        共 {total} 条 · 第 {page}/{pages} 页
      </span>
      <button className="btn sm" disabled={page <= 1} onClick={() => onChange(page - 1)}>
        上一页
      </button>
      <button className="btn sm" disabled={page >= pages} onClick={() => onChange(page + 1)}>
        下一页
      </button>
    </div>
  );
}

/** 列表的四种状态：加载、错误、无权限、空数据。每个新列表都要有。 */
export function ListState({
  loading,
  error,
  empty,
  emptyText = '暂无数据',
}: {
  loading?: boolean;
  error?: { status: number; message: string };
  empty?: boolean;
  emptyText?: string;
}) {
  if (error) {
    if (error.status === 403) return <Empty>当前角色无权查看这份数据</Empty>;
    if (error.status === 404) return <Empty>对象不存在，或不在当前组织范围内</Empty>;
    return <div className="note bad">加载失败：{error.message}</div>;
  }
  if (loading) return <div className="empty">加载中…</div>;
  if (empty) return <Empty>{emptyText}</Empty>;
  return null;
}

/** 文件上传。类型与大小由服务端裁决，这里只把失败原因显示出来。 */
export function FileUpload({
  label,
  accept,
  pending,
  onPick,
}: {
  label: string;
  accept?: string;
  pending?: boolean;
  onPick: (file: File) => void;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        type="file"
        accept={accept}
        disabled={pending}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onPick(file);
          event.target.value = '';
        }}
      />
      <span className="small muted">单文件上限 20 MiB；允许 PDF、CSV、XLSX、PNG、JPEG</span>
    </label>
  );
}

/** 三个数量分列展示：账面、未耗用占用、可用。少一个就会被当成同一个数看。 */
export function Balances({
  balance,
  outstanding,
  available,
  issued,
  unit,
}: {
  balance: string;
  outstanding: string;
  available: string;
  issued?: string;
  unit: string;
}) {
  return (
    <div className="balances">
      <span>
        账面 <b className="mono">{balance}</b>
        {unit}
      </span>
      <span>
        占用 <b className="mono">{outstanding}</b>
        {unit}
      </span>
      {issued && issued !== '0.000000' ? (
        <span className="warn-text">
          已领未耗 <b className="mono">{issued}</b>
          {unit}
        </span>
      ) : null}
      <span>
        可用 <b className="mono">{available}</b>
        {unit}
      </span>
    </div>
  );
}

/** 正式 / 探索性范围标识。导出与界面都要标出来，不能混着看。 */
export function ScopeNotice({ official, label }: { official: boolean; label: string }) {
  return (
    <div className={`note ${official ? '' : 'warn'}`}>
      {official ? '正式范围' : '探索性范围'}：{label}
      {official ? null : '。探索性数据不能直接用于正式报告。'}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function GateBanner({ gate }: { gate?: Gate }) {
  if (!gate) return null;
  if (!gate.open) {
    return (
      <div className="banner bad">
        全局执行门已关闭，排程 / 下发 / 续跑被禁用，保持与终止仍可执行：{gate.reasons.join('；')}
      </div>
    );
  }
  const blocked = Object.values(gate.blocked_stations ?? {});
  if (blocked.length) {
    return (
      <div className="banner warn">
        以下设备不可用，用到它们的批次不能下发或续跑（其他批次不受影响）：{blocked.join('；')}
      </div>
    );
  }
  return gate.degraded.length ? <div className="banner warn">适配器降级：{gate.degraded.join('；')}</div> : null;
}

export function Modal({
  title,
  children,
  footer,
  onClose,
  wide,
}: {
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  return (
    <div className="modal-back" onClick={(event) => event.target === event.currentTarget && onClose()}>
      <div className={`modal${wide ? ' wide' : ''}`} role="dialog" aria-modal="true">
        <header>
          <h2>{title}</h2>
          <button className="btn sm" onClick={onClose}>
            关闭
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer ? <footer>{footer}</footer> : null}
      </div>
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint ? <span className="small muted">{hint}</span> : null}
    </label>
  );
}

/** 数值输入。内部保留用户正在敲的原文，"0." "1e" 这类中间态不会被 Number() 吃掉。 */
export function NumberInput({
  value,
  onChange,
  className,
  invalid,
  disabled,
  placeholder,
  ariaLabel,
}: {
  value: number | '';
  onChange: (value: number | '') => void;
  className?: string;
  invalid?: boolean;
  disabled?: boolean;
  placeholder?: string;
  ariaLabel?: string;
}) {
  const [text, setText] = useState(value === '' ? '' : String(value));
  const committed = useRef<number | ''>(value);

  useEffect(() => {
    if (value !== committed.current) {
      committed.current = value;
      setText(value === '' ? '' : String(value));
    }
  }, [value]);

  const handle = (raw: string) => {
    setText(raw);
    if (raw.trim() === '') {
      committed.current = '';
      onChange('');
      return;
    }
    const parsed = Number(raw);
    if (Number.isFinite(parsed)) {
      committed.current = parsed;
      onChange(parsed);
    }
  };

  return (
    <input
      type="text"
      inputMode="decimal"
      aria-label={ariaLabel}
      className={[className, 'mono', invalid ? 'bad' : ''].filter(Boolean).join(' ')}
      value={text}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(event) => handle(event.target.value)}
      onBlur={() => setText(committed.current === '' ? '' : String(committed.current))}
    />
  );
}

export function Severity({ level }: { level: number }) {
  return <span className={`sev s${level}`} />;
}

export function Bar({ value, max, danger }: { value: number; max: number; danger?: boolean }) {
  const pct = Math.min(100, max ? (value / max) * 100 : 0);
  return (
    <div className="bar">
      <span style={{ width: `${pct}%` }} className={danger ? 'danger' : undefined} />
    </div>
  );
}

/** 破坏性操作的确认弹窗。理由必填的场景（报废、盘点）把 reasonLabel 传进来。 */
export function ConfirmDialog({
  title,
  danger,
  confirmLabel = '确认',
  children,
  reasonLabel,
  reasonPlaceholder,
  pending,
  error,
  onConfirm,
  onClose,
}: {
  title: string;
  danger?: boolean;
  confirmLabel?: string;
  children: ReactNode;
  reasonLabel?: string;
  reasonPlaceholder?: string;
  pending?: boolean;
  error?: string;
  onConfirm: (reason: string) => void;
  onClose: () => void;
}) {
  const [reason, setReason] = useState('');
  const needsReason = !!reasonLabel;

  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className={`btn ${danger ? 'danger' : 'primary'}`}
            disabled={pending || (needsReason && !reason.trim())}
            onClick={() => onConfirm(reason.trim())}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      {children}
      {needsReason ? (
        <Field label={reasonLabel} hint="写入审计记录">
          <textarea rows={2} value={reason} placeholder={reasonPlaceholder} onChange={(e) => setReason(e.target.value)} />
        </Field>
      ) : null}
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/** 服务端给出的「为什么不能做」。同一份文案也用在按钮的 title 上。 */
export function Blocked({ reasons }: { reasons: string[] }) {
  if (!reasons.length) return null;
  return (
    <ul className="blocked">
      {reasons.map((reason, index) => (
        <li key={index}>{reason}</li>
      ))}
    </ul>
  );
}

/* ---------- toast ---------- */

type ToastValue = { push: (message: string) => void };
const ToastContext = createContext<ToastValue>({ push: () => undefined });

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<{ id: number; message: string }[]>([]);
  const push = useCallback((message: string) => {
    const id = Date.now() + Math.random();
    setItems((current) => [...current, { id, message }]);
    setTimeout(() => setItems((current) => current.filter((item) => item.id !== id)), 3200);
  }, []);
  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="toast-root">
        {items.map((item) => (
          <div key={item.id} className="toast">
            {item.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastValue {
  return useContext(ToastContext);
}
