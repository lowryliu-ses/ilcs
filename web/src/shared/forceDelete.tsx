/* 测试环境的管理员级联强制删除（ILCS_ADMIN_FORCE_DELETE）。

   开关没开、或不是系统管理员时什么都不渲染。点开先看服务端预览：会连带删掉哪些对象、各删多少条；
   写原因、签名后执行，删除动作本身留审计。正式环境开关打不开。 */
import { useState } from 'react';

import { api } from './api';
import { useMutation, useQuery } from './query';
import { useSession } from './session';
import { useSignature } from './signature';
import { Blocked, Field, ListState, Modal } from './ui';

export type ForceDeleteKind =
  | 'batch'
  | 'task'
  | 'plan'
  | 'recipe'
  | 'method'
  | 'template'
  | 'station'
  | 'asset'
  | 'capability'
  | 'lot'
  | 'waste';

type Preview = {
  kind: string;
  kind_label: string;
  id: string;
  sign_target: string;
  sign_meaning: string;
  cascade: { kind: string; label: string; ids: string[] }[];
  counts: { label: string; count: number }[];
  blockers: string[];
};

export function ForceDeleteButton({
  kind,
  id,
  className = 'btn sm danger',
  onDone,
}: {
  kind: ForceDeleteKind;
  id: string;
  className?: string;
  onDone?: () => void;
}) {
  const { user } = useSession();
  const [open, setOpen] = useState(false);
  if (!user?.admin_force_delete) return null;
  return (
    <>
      <button className={className} title="测试环境：连同依赖它的数据一起删除" onClick={() => setOpen(true)}>
        强制删除
      </button>
      {open ? (
        <ForceDeleteDialog
          kind={kind}
          id={id}
          onClose={() => setOpen(false)}
          onDone={() => {
            setOpen(false);
            onDone?.();
          }}
        />
      ) : null}
    </>
  );
}

function ForceDeleteDialog({
  kind,
  id,
  onClose,
  onDone,
}: {
  kind: ForceDeleteKind;
  id: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const { sign } = useSignature();
  const [reason, setReason] = useState('');
  const path = `/admin/force-delete/${kind}/${encodeURIComponent(id)}`;
  const preview = useQuery<Preview>(`force-delete:${kind}:${id}`, () => api.get<Preview>(path));
  const body = preview.data;
  const run = useMutation(
    async () => {
      if (!body) return null;
      const signatureId = await sign(body.sign_meaning, body.sign_target, [body.sign_meaning]);
      if (!signatureId) return null;
      return api.post(path, { reason: reason.trim(), signature_id: signatureId });
    },
    { onSuccess: (result) => result && onDone() },
  );
  const blocked = !body || body.blockers.length > 0;

  return (
    <Modal
      title={`强制删除 · ${body?.kind_label ?? ''} ${id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn danger"
            disabled={blocked || !reason.trim() || run.pending}
            onClick={() => run.run().catch(() => undefined)}
          >
            {run.pending ? '删除中…' : '签名并强制删除'}
          </button>
        </>
      }
    >
      <div className="note warn">
        测试环境已开启 ILCS_ADMIN_FORCE_DELETE：连同依赖它的数据一起删除，不能恢复。审计记录与电子签名保留，
        账面库存不回滚；正式环境不能开启。
      </div>
      <ListState loading={preview.loading && !body} error={preview.error} empty={false} emptyText="" />
      {body ? (
        <>
          <Blocked reasons={body.blockers} />
          {body.cascade.length ? (
            <Field label="连带删除的对象">
              <div className="small">
                {body.cascade.map((row) => (
                  <div key={row.kind}>
                    {row.label} {row.ids.length} 个：
                    <span className="mono">{row.ids.slice(0, 12).join('、')}</span>
                    {row.ids.length > 12 ? ` 等` : null}
                  </div>
                ))}
              </div>
            </Field>
          ) : null}
          <Field label="将删除的记录">
            <div className="small">
              {body.counts.length
                ? body.counts.map((row) => `${row.label} ${row.count}`).join('，')
                : '只有对象本身'}
            </div>
          </Field>
          <Field label="原因" hint="写入审计记录">
            <textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
          </Field>
        </>
      ) : null}
      {run.error ? <div className="note bad">{run.error.message}</div> : null}
    </Modal>
  );
}
