/* 报告模板：内置的三份（完整实验报告、结果摘要、质量审计报告）只读；组织可以另建自己的模板——取舍内置章节、排顺序、
   改章节标题，再加固定文字章节（声明、适用范围、方法说明）。取数只有一套，模板只决定报告里有哪些章节、什么顺序、叫什么。

   组织模板：起草 → 发布（发布人不能是起草人，发布后冻结）→ 改要「修订」出新版本，新版本发布时旧版本退役。
   生成报告时按模板键取最新的已发布版本，章节清单写进报告快照：之后模板怎么改，已有报告都不变。 */
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { CustomReportTemplate, ReportTemplateListing, ReportTemplateSection } from '../../shared/types';
import { Blocked, ConfirmDialog, Empty, Field, ListState, Modal, Panel, Pill, useToast } from '../../shared/ui';

const KEY = 'report-templates';
const INVALIDATES = [KEY, 'reports:templates', 'audit'];

export function ReportTemplatesPage() {
  const { can, user } = useSession();
  const toast = useToast();
  const listing = useQuery<ReportTemplateListing>(KEY, () => api.get<ReportTemplateListing>('/report-templates'));
  const [editing, setEditing] = useState<CustomReportTemplate | { copyFrom: string } | null>(null);
  const [retiring, setRetiring] = useState<CustomReportTemplate | null>(null);

  const action = useMutation(
    (payload: { row: CustomReportTemplate; action: 'release' | 'revise' | 'retire' }) =>
      api.post(`/report-templates/${payload.row.id}/${payload.action}`, payload.action === 'revise' ? {} : { row_version: payload.row.row_version }),
    { invalidates: INVALIDATES },
  );
  const run = (row: CustomReportTemplate, kind: 'release' | 'revise' | 'retire', done: string) =>
    action
      .run({ row, action: kind })
      .then(() => {
        toast.push(done);
        setRetiring(null);
      })
      .catch((error) => toast.push(error.message));

  const custom = listing.data?.custom ?? [];
  const editable = can('report.edit');

  return (
    <div className="page">
      <div className="page-head">
        <h1>报告模板</h1>
        <span className="small muted">
          模板只决定报告里有哪些章节、什么顺序、叫什么，取数只有一套；组织模板还可以加固定文字章节。生成报告时章节清单写进报告快照。
        </span>
        <Link className="btn" to="/reports">
          去报告管理
        </Link>
        {editable ? (
          <button className="btn primary" onClick={() => setEditing({ copyFrom: '' })}>
            新建组织模板
          </button>
        ) : null}
      </div>

      <Panel title="内置模板（只读）" flush>
        <ListState loading={listing.loading && !listing.data} error={listing.error} />
        {listing.data ? (
          <table>
            <thead>
              <tr>
                <th>模板</th>
                <th>章节</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {listing.data.builtin.map((row) => (
                <tr key={row.key}>
                  <td>
                    {row.name} <span className="tiny muted mono">{row.key} {row.version}</span>
                    <div className="tiny muted">{row.description}</div>
                  </td>
                  <td className="small">{row.sections.map((section) => section.title).join(' → ')}</td>
                  <td className="row-end">
                    {editable ? (
                      <button className="btn sm" onClick={() => setEditing({ copyFrom: row.key })}>
                        以它为起点新建
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      <Panel title={`组织模板（${new Set(custom.map((row) => row.key)).size}）`} flush>
        {listing.data && !custom.length ? <Empty>还没有组织自己的报告模板</Empty> : null}
        {custom.length ? (
          <table>
            <thead>
              <tr>
                <th>模板</th>
                <th>状态</th>
                <th>章节</th>
                <th>起草 / 发布</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {[...custom].reverse().map((row) => (
                <tr key={row.id} className={row.state === 'retired' ? 'retired-row' : undefined}>
                  <td>
                    {row.name} <span className="tiny muted mono">{row.key} v{row.version}</span>
                    {row.description ? <div className="tiny muted">{row.description}</div> : null}
                  </td>
                  <td>
                    <Pill state={row.state === 'released' ? 'running' : row.state === 'draft' ? 'paused' : 'retired'} label={row.state_label} />
                  </td>
                  <td className="small">
                    {row.sections.map((section) => (section.kind === 'text' ? `「${section.title}」` : section.title)).join(' → ')}
                  </td>
                  <td className="tiny muted">
                    {row.created_by_name} {row.created_at ? clock(row.created_at) : ''}
                    {row.released_at ? <div>发布：{row.released_by_name} {clock(row.released_at)}</div> : null}
                  </td>
                  <td className="row-end">
                    {editable && row.state === 'draft' ? (
                      <button className="btn sm" onClick={() => setEditing(row)}>
                        编辑
                      </button>
                    ) : null}
                    {can('report.approve') && row.state === 'draft' ? (
                      <button
                        className="btn sm primary"
                        disabled={action.pending || row.created_by === user?.id}
                        title={row.created_by === user?.id ? '起草人不能发布本人起草的模板' : '发布后冻结；同一个键原来的已发布版本退役'}
                        onClick={() => run(row, 'release', '已发布：之后按这个键出的报告用这一版')}
                      >
                        发布
                      </button>
                    ) : null}
                    {editable && row.state === 'released' ? (
                      <button className="btn sm" disabled={action.pending} onClick={() => run(row, 'revise', '已修订出新版本草稿')}>
                        修订
                      </button>
                    ) : null}
                    {can('report.approve') && row.state !== 'retired' ? (
                      <button className="btn sm danger" onClick={() => setRetiring(row)}>
                        {row.state === 'draft' ? '丢弃' : '退役'}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      {editing && listing.data ? (
        <TemplateDialog
          template={'id' in editing ? editing : undefined}
          copyFrom={'copyFrom' in editing ? editing.copyFrom : ''}
          listing={listing.data}
          onClose={() => setEditing(null)}
        />
      ) : null}
      {retiring ? (
        <ConfirmDialog
          title={`${retiring.state === 'draft' ? '丢弃草稿' : '退役报告模板'} · ${retiring.key} v${retiring.version}`}
          danger
          confirmLabel={retiring.state === 'draft' ? '丢弃' : '退役'}
          pending={action.pending}
          onClose={() => setRetiring(null)}
          onConfirm={() => run(retiring, 'retire', retiring.state === 'draft' ? '草稿已丢弃' : '已退役：不能再用它出新报告')}
        >
          <div className="note warn">
            {retiring.state === 'draft'
              ? '这份草稿不再使用，记录保留。'
              : '退役后不能再用它出新报告；已有报告的章节快照不受影响，重新取数时沿用原来的章节。'}
          </div>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

/* 起草 / 编辑组织模板：章节逐条编辑——内置章节可改标题，文字章节写标题与正文；上下移、删除。 */
function TemplateDialog({
  template,
  copyFrom,
  listing,
  onClose,
}: {
  template?: CustomReportTemplate;
  copyFrom: string;
  listing: ReportTemplateListing;
  onClose: () => void;
}) {
  const toast = useToast();
  const builtinTitle = Object.fromEntries(listing.sections.map((row) => [row.key, row.title]));
  const source = listing.builtin.find((row) => row.key === copyFrom);
  const [key, setKey] = useState(template?.key ?? '');
  const [name, setName] = useState(template?.name ?? (source ? `${source.name}（本组织）` : ''));
  const [description, setDescription] = useState(template?.description ?? source?.description ?? '');
  const [sections, setSections] = useState<ReportTemplateSection[]>(
    template?.sections.map((row) => ({ ...row, title: row.kind === 'text' ? row.title : row.title === builtinTitle[row.key] ? '' : row.title }))
      ?? source?.sections.map((row) => ({ key: row.key, title: '' }))
      ?? [{ key: 'plan', title: '' }, { key: 'results', title: '' }, { key: 'conclusion', title: '' }, { key: 'approval', title: '' }],
  );
  const [adding, setAdding] = useState('');
  const [error, setError] = useState<{ message: string; problems: string[] } | null>(null);

  const save = useMutation(
    () => {
      const payload = { name: name.trim(), description, sections: sections.map((row) => (row.kind === 'text' ? row : { key: row.key, ...(row.title?.trim() ? { title: row.title.trim() } : {}) })) };
      return template
        ? api.patch(`/report-templates/${template.id}`, { ...payload, row_version: template.row_version })
        : api.post('/report-templates', { key: key.trim(), ...payload });
    },
    {
      invalidates: INVALIDATES,
      onSuccess: () => {
        toast.push(template ? '草稿已保存' : '草稿已建好：由有批准报告权限的另一个人发布后才能用');
        onClose();
      },
    },
  );
  const used = new Set(sections.map((row) => row.key));
  const move = (index: number, to: number) => {
    if (to < 0 || to >= sections.length) return;
    const next = [...sections];
    const [picked] = next.splice(index, 1);
    next.splice(to, 0, picked);
    setSections(next);
  };
  const patch = (index: number, changes: Partial<ReportTemplateSection>) =>
    setSections(sections.map((row, at) => (at === index ? { ...row, ...changes } : row)));

  return (
    <Modal
      title={template ? `编辑报告模板 · ${template.key} v${template.version}` : '新建组织报告模板'}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={save.pending || !name.trim() || !sections.length || (!template && !key.trim())}
            onClick={() =>
              save.run().catch((caught) =>
                setError({
                  message: caught.message,
                  problems: ((caught.payload as { detail?: { problems?: string[] } } | undefined)?.detail?.problems) ?? [],
                }),
              )
            }
          >
            保存草稿
          </button>
        </>
      }
    >
      <div className="grid cols-3">
        <Field label="模板键" hint={template ? '建好后不可更改' : '小写字母开头，如 rd-internal；不能与内置模板重名'}>
          <input className="mono" value={key} disabled={!!template} onChange={(event) => setKey(event.target.value)} />
        </Field>
        <Field label="名称">
          <input value={name} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field label="说明">
          <input value={description} onChange={(event) => setDescription(event.target.value)} />
        </Field>
      </div>

      <div className="small muted">章节（按这个顺序印在报告里；内置章节标题留空就用原标题）</div>
      <div className="stack">
        {sections.map((row, index) => (
          <div key={`${row.key}-${index}`} className="note" style={{ display: 'grid', gap: 6 }}>
            <div className="row">
              <b className="small">{index + 1}.</b>
              {row.kind === 'text' ? (
                <span className="tag">文字章节</span>
              ) : (
                <span className="small">{builtinTitle[row.key] ?? row.key}</span>
              )}
              <input
                style={{ minWidth: 220 }}
                value={row.title ?? ''}
                placeholder={row.kind === 'text' ? '标题（必填）' : `标题（留空为「${builtinTitle[row.key] ?? row.key}」）`}
                onChange={(event) => patch(index, { title: event.target.value })}
              />
              <span style={{ flex: 1 }} />
              <button className="btn sm" disabled={index === 0} onClick={() => move(index, index - 1)}>↑</button>
              <button className="btn sm" disabled={index === sections.length - 1} onClick={() => move(index, index + 1)}>↓</button>
              <button className="btn sm danger" onClick={() => setSections(sections.filter((_, at) => at !== index))}>删</button>
            </div>
            {row.kind === 'text' ? (
              <textarea rows={3} value={row.body ?? ''} placeholder="正文：原样印在报告里，一行一段"
                onChange={(event) => patch(index, { body: event.target.value })} />
            ) : null}
          </div>
        ))}
      </div>
      <div className="row">
        <select value={adding} onChange={(event) => setAdding(event.target.value)} aria-label="加一个内置章节">
          <option value="">加一个内置章节</option>
          {listing.sections.filter((row) => !used.has(row.key)).map((row) => (
            <option key={row.key} value={row.key}>
              {row.title}
            </option>
          ))}
        </select>
        <button className="btn sm" disabled={!adding} onClick={() => {
          setSections([...sections, { key: adding, title: '' }]);
          setAdding('');
        }}>
          加入
        </button>
        <button className="btn sm" onClick={() => {
          let index = 1;
          while (used.has(`text:${index}`)) index += 1;
          setSections([...sections, { kind: 'text', key: `text:${index}`, title: '', body: '' }]);
        }}>
          加一个文字章节
        </button>
      </div>
      {error ? (
        <div className="note bad">
          {error.message}
          <Blocked reasons={error.problems} />
        </div>
      ) : null}
    </Modal>
  );
}
