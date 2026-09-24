import { useState } from 'react';

import { api, pageQuery } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import { useNavigate } from 'react-router-dom';

import { CommentsPanel } from '../../shared/comments';
import type { CapabilityRow, DiffRow, Paged, SopStep, SopVersionRow } from '../../shared/types';
import {
  ConfirmDialog, Empty, Field, FileUpload, ListState, Modal, Pager, Panel, Pill, useToast,
} from '../../shared/ui';

const STATES: [string, string][] = [
  ['draft', '草稿'],
  ['review', '评审中'],
  ['published', '已发布'],
  ['retired', '已退役'],
];

export function SopsPage() {
  const { can } = useSession();
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [state, setState] = useState('');
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const query = pageQuery({ page, page_size: 20, state });
  const versions = useQuery<Paged<SopVersionRow>>(
    `sops:${query}`, () => api.get<Paged<SopVersionRow>>(`/sops${query}`),
  );

  const rows = versions.data?.items ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>SOP 规程</h1>
        <span className="small muted">
          已发布内容不可原位编辑；修订和恢复历史内容都产生新版本。新版本生效不自动改在途运行，
          只显示影响并由负责人决定。
        </span>
      </div>

      <Panel
        title={`SOP 版本（${versions.data?.total ?? 0}）`}
        aside={
          <div className="filters">
            <select value={state} onChange={(event) => { setState(event.target.value); setPage(1); }}>
              <option value="">全部状态</option>
              {STATES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            {can('sop.edit') ? (
              <button className="btn primary sm" onClick={() => setCreating(true)}>
                新建版本
              </button>
            ) : null}
          </div>
        }
        flush
      >
        <ListState
          loading={versions.loading && !versions.data}
          error={versions.error}
          empty={!rows.length}
          emptyText="没有符合条件的 SOP 版本"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>编号</th>
                <th>标题</th>
                <th>版本</th>
                <th>适用能力</th>
                <th>状态</th>
                <th>生效</th>
                <th>附件</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.code}</td>
                  <td>{row.title}</td>
                  <td className="mono small">{row.version}</td>
                  <td className="small">
                    {row.capability_scope.length ? row.capability_scope.join('、') : '全部'}
                    {row.requires_training_ack ? (
                      <div className="tiny warn-text">需阅读确认（{row.ack_count} 人已确认）</div>
                    ) : null}
                  </td>
                  <td>
                    <Pill state={row.state} label={row.state_label} />
                  </td>
                  <td className="small">{row.effective_from ? clock(row.effective_from) : '—'}</td>
                  <td className="small">
                    {row.file_id ? (
                      <button
                        className="btn sm"
                        onClick={() =>
                          api
                            .download(`/files/${row.file_id}/download`, row.filename || `${row.code}.pdf`)
                            .catch((error) => toast.push(error.message))
                        }
                      >
                        下载
                      </button>
                    ) : (
                      <span className="muted">未上传</span>
                    )}
                  </td>
                  <td className="row-end">
                    <button className="btn sm" onClick={() => setSelected(row.id)}>
                      详情
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <Pager
          page={versions.data?.page ?? 1}
          pageSize={versions.data?.page_size ?? 20}
          total={versions.data?.total ?? 0}
          onChange={setPage}
        />
      </Panel>

      {creating ? <CreateDialog onClose={() => setCreating(false)} /> : null}
      {selected ? <DetailDialog versionId={selected} onClose={() => setSelected(null)} /> : null}
    </div>
  );
}

function DetailDialog({ versionId, onClose }: { versionId: string; onClose: () => void }) {
  const { can } = useSession();
  const toast = useToast();
  const { sign } = useSignature();
  const detail = useQuery<SopVersionRow>(`sops:${versionId}`, () => api.get<SopVersionRow>(`/sops/${versionId}`));
  const [retiring, setRetiring] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editingSteps, setEditingSteps] = useState(false);
  const [against, setAgainst] = useState('');
  const navigate = useNavigate();
  const siblings = useQuery<Paged<SopVersionRow>>('sops:siblings', () => api.get<Paged<SopVersionRow>>('/sops?page_size=100'));

  const invalidates = ['sops', 'recipes', 'dashboard'];
  const generate = useMutation(
    () => api.post<{ recipe_id: string; sop_linked: boolean }>(`/sops/${versionId}/generate-recipe`, { plate: 8 }),
    {
      invalidates: ['recipes'],
      onSuccess: (result) => {
        toast.push(result.sop_linked ? '已生成流程草稿并挂接本 SOP 版本' : '已生成流程草稿（SOP 未发布，未挂接）');
        navigate(`/recipes/${result.recipe_id}/edit`);
      },
    },
  );
  const restore = useMutation(() => api.post<SopVersionRow>(`/sops/${versionId}/restore`, {}), {
    invalidates,
    onSuccess: (row) => toast.push(`已从此版本恢复出新草稿 ${row.version}；历史版本不变`),
  });
  const submit = useMutation(() => api.post(`/sops/${versionId}/submit`), {
    invalidates,
    onSuccess: () => toast.push('已提交评审'),
  });
  const decide = useMutation(
    (payload: { conclusion: string; reason?: string; signature_id?: string }) =>
      api.post(`/sops/${versionId}/decision`, {
        ...payload,
        effective_from: payload.conclusion === 'approved' ? new Date().toISOString() : undefined,
      }),
    {
      invalidates,
      onSuccess: (result) => {
        const row = result as SopVersionRow & { impacted_recipes?: { id: string }[] };
        toast.push(
          row.state === 'published'
            ? `已发布；影响 ${row.impacted_recipes?.length ?? 0} 个流程，在途运行不自动改版`
            : '已驳回回草稿',
        );
        setRejecting(false);
      },
    },
  );
  const retire = useMutation((reason: string) => api.post(`/sops/${versionId}/retire`, { reason }), {
    invalidates,
    onSuccess: () => {
      toast.push('已退役；历史引用与附件保持可查');
      setRetiring(false);
    },
  });
  const acknowledge = useMutation(() => api.post(`/sops/${versionId}/acknowledge`), {
    invalidates,
    onSuccess: (result) =>
      toast.push((result as { replayed: boolean }).replayed ? '已确认过' : '阅读确认已记录'),
  });

  const version = detail.data;

  return (
    <Modal title={`SOP · ${version?.code ?? ''} ${version?.version ?? ''}`} onClose={onClose} wide>
      {version ? (
        <>
          <div className="metrics">
            <div className="metric">
              <span className="metric-label">状态</span>
              <strong className="metric-value">
                <Pill state={version.state} label={version.state_label} />
              </strong>
              <span className="metric-hint">{version.editable ? '草稿可编辑' : '已发布内容只读'}</span>
            </div>
            <div className="metric">
              <span className="metric-label">作者 / 批准</span>
              <strong className="metric-value">{version.author_name || '—'}</strong>
              <span className="metric-hint">批准 {version.approver_name || '未批准'}</span>
            </div>
            <div className="metric">
              <span className="metric-label">附件摘要</span>
              <strong className="metric-value mono" style={{ fontSize: 13 }}>
                {version.file_checksum ? `${version.file_checksum.slice(0, 12)}…` : '无附件'}
              </strong>
              <span className="metric-hint">批次会固化这个摘要</span>
            </div>
          </div>

          {version.reject_reason ? <div className="note warn">驳回理由：{version.reject_reason}</div> : null}

          <div className="panel-aside" style={{ justifyContent: 'flex-end', margin: '10px 0' }}>
            {version.state === 'draft' && can('sop.edit') ? (
              <button className="btn sm" onClick={() => setEditing(true)}>
                编辑草稿
              </button>
            ) : null}
            {version.state === 'draft' && can('sop.edit') ? (
              <button
                className="btn sm"
                disabled={submit.pending || !version.file_id}
                title={version.file_id ? undefined : '提交评审前必须上传附件'}
                onClick={() => submit.run().catch((error) => toast.push(error.message))}
              >
                提交评审
              </button>
            ) : null}
            {version.state === 'review' && can('sop.approve') ? (
              <>
                <button className="btn sm" onClick={() => setRejecting(true)}>
                  驳回
                </button>
                <button
                  className="btn primary sm"
                  onClick={() =>
                    sign('批准并发布 SOP', version.id, ['批准并发布'], version.row_version)
                      .then((signatureId) =>
                        signatureId ? decide.run({ conclusion: 'approved', signature_id: signatureId }) : undefined,
                      )
                      .catch((error) => toast.push(error.message))
                  }
                >
                  批准并发布
                </button>
              </>
            ) : null}
            {version.state === 'published' && version.requires_training_ack ? (
              <button
                className="btn sm"
                disabled={acknowledge.pending}
                onClick={() => acknowledge.run().catch((error) => toast.push(error.message))}
              >
                阅读确认
              </button>
            ) : null}
            {version.state === 'published' && can('sop.approve') ? (
              <button className="btn sm danger" onClick={() => setRetiring(true)}>
                退役
              </button>
            ) : null}
            {version.steps.length && can('recipe.edit') ? (
              <button className="btn sm" disabled={generate.pending} onClick={() => generate.run().catch((error) => toast.push(error.message))}>
                生成流程草稿
              </button>
            ) : null}
            {['published', 'retired'].includes(version.state) && can('sop.edit') ? (
              <button className="btn sm" disabled={restore.pending} onClick={() => restore.run().catch((error) => toast.push(error.message))}>
                从此版本恢复
              </button>
            ) : null}
          </div>
          {version.restored_from ? <div className="tiny muted">本版本内容恢复自历史版本 {version.restored_from.slice(0, 8)}</div> : null}

          <Panel
            title={`结构化步骤（${version.steps.length}）`}
            aside={
              version.state === 'draft' && can('sop.edit') ? (
                <button className="btn sm" onClick={() => setEditingSteps(true)}>
                  编辑步骤
                </button>
              ) : null
            }
            flush
          >
            {version.steps.length ? (
              <table>
                <tbody>
                  {version.steps.map((step, index) => (
                    <tr key={index}>
                      <td className="mono small">{index + 1}</td>
                      <td>
                        <b>{step.title}</b> <span className="tag">{STEP_KIND_LABEL[step.kind]}</span>
                        {step.instructions ? <div className="tiny muted">{step.instructions}</div> : null}
                        {step.checks.length ? <div className="tiny">核对：{step.checks.join('；')}</div> : null}
                      </td>
                      <td className="small mono">
                        {step.kind === 'device' ? `${step.capability} ${Object.entries(step.params).map(([k, v]) => `${k}=${v}`).join(' ')}` : ''}
                      </td>
                      <td className="small">{step.duration_min ? `${step.duration_min} min` : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>还没有结构化步骤；写上之后可以一键生成流程草稿</Empty>
            )}
          </Panel>

          <Panel
            title="版本对比"
            aside={
              <select value={against} onChange={(event) => setAgainst(event.target.value)}>
                <option value="">选择同编号的另一个版本</option>
                {(siblings.data?.items ?? [])
                  .filter((row) => row.sop_id === version.sop_id && row.id !== version.id)
                  .map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.version}（{row.state_label}）
                    </option>
                  ))}
              </select>
            }
          >
            {against ? <SopDiff versionId={version.id} against={against} /> : <div className="small muted">选一个版本查看附件、适用范围与步骤的差异</div>}
          </Panel>

          <Panel title="引用该版本的流程" flush>
            {version.using_recipes?.length ? (
              <table>
                <tbody>
                  {version.using_recipes.map((row) => (
                    <tr key={row.id}>
                      <td className="mono">{row.id}</td>
                      <td>{row.name}</td>
                      <td className="small">v{row.version}</td>
                      <td>
                        <Pill state={row.state} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>还没有流程引用它</Empty>
            )}
          </Panel>

          {version.requires_training_ack ? (
            <Panel title={`阅读确认（${version.acks?.length ?? 0}）`} flush>
              {version.acks?.length ? (
                <table>
                  <tbody>
                    {version.acks.map((row) => (
                      <tr key={row.person_id}>
                        <td>{row.person_name}</td>
                        <td className="small">{clock(row.acked_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty>还没有人确认；未确认的人员不能执行相关节点</Empty>
              )}
            </Panel>
          ) : null}
        </>
      ) : (
        <ListState loading={detail.loading} error={detail.error} />
      )}

      {version ? <CommentsPanel targetType="sop_version" targetId={version.id} /> : null}
      {editing && version ? (
        <DraftEditDialog version={version} onClose={() => setEditing(false)} />
      ) : null}
      {editingSteps && version ? <StepsEditor version={version} onClose={() => setEditingSteps(false)} /> : null}
      {retiring ? (
        <ConfirmDialog
          title="退役 SOP 版本"
          danger
          confirmLabel="退役"
          reasonLabel="退役理由"
          pending={retire.pending}
          error={retire.error?.message}
          onConfirm={(reason) => retire.run(reason).catch(() => undefined)}
          onClose={() => setRetiring(false)}
        >
          <div className="note warn">
            退役后新任务必须改用有效版本；历史引用与附件仍可查，在途运行不自动改版。
          </div>
        </ConfirmDialog>
      ) : null}
      {rejecting ? (
        <ConfirmDialog
          title="驳回 SOP 版本"
          confirmLabel="驳回"
          reasonLabel="驳回理由"
          pending={decide.pending}
          error={decide.error?.message}
          onConfirm={(reason) => decide.run({ conclusion: 'rejected', reason }).catch(() => undefined)}
          onClose={() => setRejecting(false)}
        >
          <div className="note">驳回后回到草稿，可以修改后重新提交。</div>
        </ConfirmDialog>
      ) : null}
    </Modal>
  );
}

/* 改草稿。已发布版本一律只读——受控文件的内容与版本号是一对一的，
   改内容不换版本号，现场按纸质版作业的人就无从知道自己看的是哪一版。
   要改已发布的内容，走「同编号新建版本」。 */
function DraftEditDialog({ version, onClose }: { version: SopVersionRow; onClose: () => void }) {
  const toast = useToast();
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const [scope, setScope] = useState<string[]>(version.capability_scope ?? []);
  const [sampleTypes, setSampleTypes] = useState((version.sample_types ?? []).join('、'));
  const [needsAck, setNeedsAck] = useState(version.requires_training_ack);
  const [file, setFile] = useState<{ id: string; filename: string } | null>(
    version.file_id ? { id: version.file_id, filename: version.filename } : null,
  );

  const upload = useMutation(
    (picked: File) => api.upload<{ id: string; filename: string }>('/files', picked, { ref_type: 'sop' }),
    { onSuccess: (result) => setFile(result) },
  );

  const save = useMutation(
    () =>
      api.patch(`/sops/${version.id}`, {
        file_id: file?.id ?? '',
        capability_scope: scope,
        sample_types: sampleTypes.split(/[、,，\s]+/).map((item) => item.trim()).filter(Boolean),
        requires_training_ack: needsAck,
        row_version: version.row_version,
      }),
    {
      invalidates: ['sops', 'recipes'],
      onSuccess: () => {
        toast.push('草稿已更新');
        onClose();
      },
    },
  );

  return (
    <Modal
      title={`编辑草稿 · ${version.code} ${version.version}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={save.pending} onClick={() => save.run().catch(() => undefined)}>
            保存
          </button>
        </>
      }
    >
      <div className="note">
        编号、标题与版本号不在这里改：它们是这份受控文件的身份。要换身份请新建版本。
      </div>
      <FileUpload
        label="SOP 附件（换附件会改变摘要）"
        accept="application/pdf"
        pending={upload.pending}
        onPick={(picked) => upload.run(picked).catch((error) => toast.push(error.message))}
      />
      <div className="note">
        {file ? `当前附件：${file.filename}` : '当前没有附件；提交评审前必须上传'}
      </div>
      {upload.error ? <div className="note bad">{upload.error.message}</div> : null}
      <Field label="适用能力" hint="不选表示适用全部能力">
        <div className="chips">
          {(capabilities.data ?? []).map((row) => (
            <label key={row.id} className={`chip${scope.includes(row.id) ? ' on' : ''}`}>
              <input
                type="checkbox"
                checked={scope.includes(row.id)}
                onChange={(event) =>
                  setScope(
                    event.target.checked
                      ? [...scope, row.id]
                      : scope.filter((item) => item !== row.id),
                  )
                }
              />
              {row.name}
            </label>
          ))}
        </div>
      </Field>
      <Field label="适用样本类型" hint="顿号或逗号分隔；留空表示不限">
        <input value={sampleTypes} onChange={(event) => setSampleTypes(event.target.value)} />
      </Field>
      <Field label="培训要求">
        <label className="small">
          <input type="checkbox" checked={needsAck} onChange={(event) => setNeedsAck(event.target.checked)} />
          需要阅读确认后才能执行引用它的流程
        </label>
      </Field>
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}

function CreateDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const [form, setForm] = useState({ code: '', title: '', version: '' });
  const [scope, setScope] = useState<string[]>([]);
  const [needsAck, setNeedsAck] = useState(false);
  const [file, setFile] = useState<{ id: string; filename: string } | null>(null);

  const upload = useMutation(
    (picked: File) => api.upload<{ id: string; filename: string }>('/files', picked, { ref_type: 'sop' }),
    { onSuccess: (result) => setFile(result) },
  );
  const create = useMutation(
    () =>
      api.post('/sops', {
        ...form,
        file_id: file?.id ?? '',
        capability_scope: scope,
        requires_training_ack: needsAck,
      }),
    {
      invalidates: ['sops'],
      onSuccess: () => {
        toast.push('SOP 版本已创建（草稿）');
        onClose();
      },
    },
  );

  return (
    <Modal
      title="新建 SOP 版本"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!form.code || !form.title || create.pending}
            onClick={() => create.run().catch(() => undefined)}
          >
            创建草稿
          </button>
        </>
      }
    >
      <div className="note">
        首期只做文件上传与阅读确认，不建在线富文本编辑器。同一编号新建版本即为修订。
      </div>
      <Field label="SOP 编号">
        <input value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} />
      </Field>
      <Field label="标题">
        <input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} />
      </Field>
      <Field label="版本" hint="留空按现有版本数自动编号">
        <input value={form.version} onChange={(event) => setForm({ ...form, version: event.target.value })} />
      </Field>
      <Field label="适用能力" hint="不选表示适用全部能力">
        <select
          multiple
          size={5}
          value={scope}
          onChange={(event) =>
            setScope(Array.from(event.target.selectedOptions).map((option) => option.value))
          }
        >
          {(capabilities.data ?? []).map((row) => (
            <option key={row.id} value={row.id}>
              {row.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="要求阅读确认">
        <label className="small">
          <input type="checkbox" checked={needsAck} onChange={(event) => setNeedsAck(event.target.checked)} />
          执行相关节点前必须有该版本的阅读确认或等效资质
        </label>
      </Field>
      <FileUpload
        label="SOP 附件"
        accept="application/pdf"
        pending={upload.pending}
        onPick={(picked) => upload.run(picked).catch((error) => toast.push(error.message))}
      />
      {file ? <div className="note">已上传 {file.filename}</div> : null}
      {upload.error ? <div className="note bad">{upload.error.message}</div> : null}
      {create.error ? <div className="note bad">{create.error.message}</div> : null}
    </Modal>
  );
}

const STEP_KIND_LABEL: Record<SopStep['kind'], string> = { device: '设备', manual: '人工', wait: '等待', review: '审核' };

function SopDiff({ versionId, against }: { versionId: string; against: string }) {
  const diff = useQuery<{ from: string; to: string; changes: DiffRow[] }>(`sops:diff:${against}:${versionId}`, () =>
    api.get(`/sops/${versionId}/diff?against=${against}`),
  );
  if (!diff.data) return <div className="small muted">{diff.error ? diff.error.message : '加载中…'}</div>;
  if (!diff.data.changes.length) return <div className="small muted">{diff.data.from} 与 {diff.data.to} 内容相同</div>;
  return (
    <table>
      <thead>
        <tr>
          <th>项目</th>
          <th>{diff.data.from}</th>
          <th>{diff.data.to}</th>
        </tr>
      </thead>
      <tbody>
        {diff.data.changes.map((row) => (
          <tr key={row.field}>
            <td className="small">{row.label}</td>
            <td className="tiny mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{row.before}</td>
            <td className="tiny mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{row.after}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 数字 SOP 的步骤编辑：人照着做什么。设备步骤写能力与参数，人工步骤写说明与逐项核对。 */
function StepsEditor({ version, onClose }: { version: SopVersionRow; onClose: () => void }) {
  const toast = useToast();
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const [steps, setSteps] = useState<SopStep[]>(version.steps.length ? version.steps : []);
  const save = useMutation(() => api.put(`/sops/${version.id}/steps`, { steps, row_version: version.row_version }), {
    invalidates: ['sops'],
    onSuccess: () => {
      toast.push('结构化步骤已保存');
      onClose();
    },
  });
  const update = (index: number, change: Partial<SopStep>) =>
    setSteps((current) => current.map((row, at) => (at === index ? { ...row, ...change } : row)));
  const blank: SopStep = { title: '', kind: 'manual', capability: '', params: {}, duration_min: 0, instructions: '', checks: [] };
  return (
    <Modal
      title={`结构化步骤 · ${version.code} ${version.version}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={save.pending || steps.some((row) => !row.title.trim())} onClick={() => save.run().catch(() => undefined)}>
            保存
          </button>
        </>
      }
    >
      {steps.map((step, index) => {
        const capability = (capabilities.data ?? []).find((row) => row.id === step.capability);
        return (
          <div key={index} className="panel-body" style={{ border: '1px solid var(--border)', borderRadius: 6, marginBottom: 8 }}>
            <div className="filters">
              <span className="mono small">{index + 1}</span>
              <input placeholder="步骤标题" value={step.title} onChange={(event) => update(index, { title: event.target.value })} />
              <select value={step.kind} onChange={(event) => update(index, { kind: event.target.value as SopStep['kind'] })}>
                {Object.entries(STEP_KIND_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
              <input
                type="number"
                min={0}
                style={{ width: 80 }}
                value={step.duration_min}
                onChange={(event) => update(index, { duration_min: Number(event.target.value) || 0 })}
              />
              <span className="small muted">min</span>
              <button className="btn sm" onClick={() => setSteps(steps.filter((_, at) => at !== index))}>
                删除
              </button>
            </div>
            {step.kind === 'device' ? (
              <div className="filters">
                <select value={step.capability} onChange={(event) => update(index, { capability: event.target.value, params: {} })}>
                  <option value="">选择能力</option>
                  {(capabilities.data ?? []).filter((row) => !row.retired).map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.name}
                    </option>
                  ))}
                </select>
                {Object.entries(capability?.params ?? {}).map(([key, label]) => (
                  <label key={key} className="small">
                    {label}{' '}
                    <input
                      type="number"
                      style={{ width: 80 }}
                      value={step.params[key] ?? ''}
                      onChange={(event) =>
                        update(index, {
                          params: event.target.value === ''
                            ? Object.fromEntries(Object.entries(step.params).filter(([name]) => name !== key))
                            : { ...step.params, [key]: Number(event.target.value) },
                        })
                      }
                    />
                  </label>
                ))}
              </div>
            ) : null}
            <textarea
              rows={2}
              placeholder="操作说明"
              value={step.instructions}
              onChange={(event) => update(index, { instructions: event.target.value })}
            />
            {step.kind === 'manual' ? (
              <input
                placeholder="逐项核对（用；分隔），生成流程时每项变成一个勾选项"
                value={step.checks.join('；')}
                onChange={(event) => update(index, { checks: event.target.value.split(/[；;]/).map((text) => text.trim()).filter(Boolean) })}
              />
            ) : null}
          </div>
        );
      })}
      <button className="btn sm" onClick={() => setSteps([...steps, { ...blank }])}>
        增加步骤
      </button>
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}
