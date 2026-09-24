/* 批注：方案、SOP 版本、方法、报告版本上的评审意见。
   批注记下它针对的对象版本；对象修订后，旧批注仍标着它说的是哪一版。解决后保留，不删除。 */
import { useState } from 'react';

import { api } from './api';
import { clock } from './format';
import { useMutation, useQuery } from './query';
import { Panel, useToast } from './ui';

export type CommentRow = {
  id: string;
  target_type: string;
  target_id: string;
  target_version: string;
  anchor: string;
  body: string;
  author_name: string;
  created_at: string;
  resolved: boolean;
  resolved_by: string;
  resolved_at: string | null;
};

export function CommentsPanel({
  targetType,
  targetId,
  anchors,
}: {
  targetType: 'plan' | 'sop_version' | 'recipe' | 'report_version';
  targetId: string;
  /** 可选的批注位置（字段 / 章节），如 [['goal', '目的']] */
  anchors?: [string, string][];
}) {
  const toast = useToast();
  const key = `comments:${targetType}:${targetId}`;
  const comments = useQuery<CommentRow[]>(key, () =>
    api.get<CommentRow[]>(`/comments?target_type=${targetType}&target_id=${encodeURIComponent(targetId)}`),
  );
  const [body, setBody] = useState('');
  const [anchor, setAnchor] = useState('');
  const [showResolved, setShowResolved] = useState(false);
  const post = useMutation(() => api.post('/comments', { target_type: targetType, target_id: targetId, anchor, body }), {
    invalidates: [key],
    onSuccess: () => setBody(''),
  });
  const resolve = useMutation((id: string) => api.post(`/comments/${id}/resolve`), { invalidates: [key] });
  const rows = (comments.data ?? []).filter((row) => showResolved || !row.resolved);
  const open = (comments.data ?? []).filter((row) => !row.resolved).length;
  const anchorLabel = Object.fromEntries(anchors ?? []);
  return (
    <Panel
      title={`批注（${open} 条待处理）`}
      aside={
        <label className="small">
          <input type="checkbox" checked={showResolved} onChange={(event) => setShowResolved(event.target.checked)} /> 显示已解决
        </label>
      }
    >
      {rows.length ? (
        <ul className="check-list">
          {rows.map((row) => (
            <li key={row.id} className={row.resolved ? 'na' : 'ok'}>
              <span className="check-mark">{row.resolved ? '✓' : '·'}</span>
              <div>
                <div className="small">
                  <b>{row.author_name}</b> <span className="tiny muted">{clock(row.created_at)} · 针对 {row.target_version}</span>
                  {row.anchor ? <span className="tag">{anchorLabel[row.anchor] ?? row.anchor}</span> : null}
                </div>
                <div className="small" style={{ whiteSpace: 'pre-wrap' }}>{row.body}</div>
                {row.resolved ? (
                  <div className="tiny muted">{row.resolved_by} 已解决 {row.resolved_at ? clock(row.resolved_at) : ''}</div>
                ) : (
                  <button className="btn sm" disabled={resolve.pending} onClick={() => resolve.run(row.id).catch((error) => toast.push(error.message))}>
                    标记已解决
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <div className="small muted">{comments.loading ? '加载中…' : '没有批注'}</div>
      )}
      <div className="filters" style={{ alignItems: 'flex-start' }}>
        {anchors?.length ? (
          <select value={anchor} onChange={(event) => setAnchor(event.target.value)}>
            <option value="">整体</option>
            {anchors.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        ) : null}
        <textarea rows={2} style={{ flex: 1 }} placeholder="写批注…" value={body} onChange={(event) => setBody(event.target.value)} />
        <button className="btn sm primary" disabled={!body.trim() || post.pending} onClick={() => post.run().catch((error) => toast.push(error.message))}>
          发表
        </button>
      </div>
    </Panel>
  );
}
