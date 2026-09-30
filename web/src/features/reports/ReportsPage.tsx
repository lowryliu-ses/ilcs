import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api, pageQuery } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import { CommentsPanel } from '../../shared/comments';
import { BATCH_STATE_LABEL } from '../../shared/types';
import type { BatchSummary, Paged, ReportContent, ReportTemplate, ReportVersionRow, TaskRow } from '../../shared/types';
import {
  Blocked, ConfirmDialog, Empty, Field, ListState, Modal, Pager, Panel, Pill, useToast,
} from '../../shared/ui';

const STATES: [string, string][] = [
  ['draft', '草稿'],
  ['review', '评审中'],
  ['approved', '已批准'],
  ['published', '已发布'],
  ['superseded', '已被替代'],
];

export function ReportsPage() {
  const { can } = useSession();
  const [page, setPage] = useState(1);
  const [state, setState] = useState('');
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const query = pageQuery({ page, page_size: 20, state });
  const reports = useQuery<Paged<ReportVersionRow>>(
    `reports:${query}`, () => api.get<Paged<ReportVersionRow>>(`/reports${query}`),
  );

  const rows = reports.data?.items ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>报告管理</h1>
        <span className="small muted">
          正式报告只纳入审核通过且质量有效的结果；无效或可疑的结果作为已审核的排除说明出现，
          不进入正式结论统计。发布后只读，源结果修订要发布新版本并标明替代关系。
        </span>
        <Link className="btn" to="/report-templates">
          报告模板
        </Link>
      </div>

      <Panel
        title={`报告版本（${reports.data?.total ?? 0}）`}
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
            {can('report.edit') ? (
              <button className="btn primary sm" onClick={() => setCreating(true)}>
                生成报告
              </button>
            ) : null}
          </div>
        }
        flush
      >
        <ListState
          loading={reports.loading && !reports.data}
          error={reports.error}
          empty={!rows.length}
          emptyText="还没有报告"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>编号</th>
                <th>标题</th>
                <th>版本</th>
                <th>批次</th>
                <th>状态</th>
                <th>编写 / 批准</th>
                <th>发布</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.code}</td>
                  <td>{row.title}</td>
                  <td className="small">
                    v{row.version}
                    {row.supersedes_id ? <div className="tiny muted">替代上一版本</div> : null}
                  </td>
                  <td className="mono small">
                    {row.batch_id || (row.task_id ? <>{row.task_id}<div className="tiny muted">父任务合并</div></> : '—')}
                  </td>
                  <td>
                    <Pill state={row.state} label={row.state_label} />
                  </td>
                  <td className="small">
                    {row.author_name || '—'}
                    <div className="tiny muted">{row.approver_name || '未批准'}</div>
                  </td>
                  <td className="small">{row.published_at ? clock(row.published_at) : '—'}</td>
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
          page={reports.data?.page ?? 1}
          pageSize={reports.data?.page_size ?? 20}
          total={reports.data?.total ?? 0}
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
  const detail = useQuery<ReportVersionRow>(
    `reports:${versionId}`, () => api.get<ReportVersionRow>(`/reports/${versionId}`),
  );
  const check = useQuery<{ ok: boolean; blocked: { key: string; label: string }[] }>(
    `reports:check:${versionId}`,
    () => api.get<{ ok: boolean; blocked: { key: string; label: string }[] }>(
      `/reports/${versionId}/publish-check`,
    ),
  );
  const [rejecting, setRejecting] = useState(false);
  const [conclusion, setConclusion] = useState('');

  const invalidates = ['reports', 'dashboard', 'tasks'];
  const save = useMutation(
    () => api.patch(`/reports/${versionId}`, { conclusion, row_version: detail.data?.row_version }),
    { invalidates, onSuccess: () => toast.push('结论已保存') },
  );
  const refresh = useMutation(
    () => api.patch(`/reports/${versionId}`, { refresh: true, row_version: detail.data?.row_version }),
    { invalidates, onSuccess: () => toast.push('已按当前已审核结果重新取数') },
  );
  const templates = useQuery<ReportTemplate[]>('reports:templates', () => api.get<ReportTemplate[]>('/reports/templates'));
  const switchTemplate = useMutation(
    (key: string) => api.patch(`/reports/${versionId}`, { template: key, row_version: detail.data?.row_version }),
    { invalidates, onSuccess: () => toast.push('已切换模板：章节随之变化，数据不变') },
  );
  const submit = useMutation(() => api.post(`/reports/${versionId}/submit`), {
    invalidates,
    onSuccess: () => toast.push('已提交审核'),
  });
  const approve = useMutation(
    (payload: { conclusion: string; reason?: string; signature_id?: string }) =>
      api.post(`/reports/${versionId}/approve`, payload),
    {
      invalidates,
      onSuccess: (result) => {
        toast.push((result as ReportVersionRow).state === 'approved' ? '已批准' : '已退回草稿');
        setRejecting(false);
      },
    },
  );
  const publish = useMutation(
    (signatureId: string) => api.post(`/reports/${versionId}/publish`, { signature_id: signatureId }, true),
    { invalidates, onSuccess: () => toast.push('已发布并固化结果版本、算法、模板与签名') },
  );
  const revise = useMutation(() => api.post(`/reports/${versionId}/revisions`), {
    invalidates,
    onSuccess: () => {
      toast.push('已基于当前结果生成新版本草稿');
      onClose();
    },
  });

  const version = detail.data;
  const content = version?.content;

  return (
    <Modal title={`报告 · ${version?.code ?? ''} v${version?.version ?? ''}`} onClose={onClose} wide>
      {version ? (
        <>
          <div className="metrics">
            <div className="metric">
              <span className="metric-label">状态</span>
              <strong className="metric-value">
                <Pill state={version.state} label={version.state_label} />
              </strong>
              <span className="metric-hint">{version.readonly ? '已发布内容只读' : '草稿可编辑'}</span>
            </div>
            <div className="metric">
              <span className="metric-label">模板 / 算法</span>
              <strong className="metric-value" style={{ fontSize: 14 }}>
                {version.template_version}
              </strong>
              <span className="metric-hint">算法 {version.algorithm_version}</span>
            </div>
            <div className="metric">
              <span className="metric-label">编写 / 批准</span>
              <strong className="metric-value" style={{ fontSize: 14 }}>
                {version.author_name || '—'}
              </strong>
              <span className="metric-hint">批准 {version.approver_name || '未批准'}</span>
            </div>
          </div>

          {version.reject_reason ? <div className="note warn">退回理由：{version.reject_reason}</div> : null}
          {check.data && !check.data.ok ? (
            <div className="note bad">
              引用结果尚未全部通过审核，不能提交或发布：
              <Blocked reasons={check.data.blocked.map((row) => row.label)} />
            </div>
          ) : null}

          <div className="panel-aside" style={{ justifyContent: 'flex-end', margin: '10px 0' }}>
            {version.state === 'draft' && can('report.edit') ? (
              <>
                <select
                  value={content?.template?.key ?? 'standard'}
                  disabled={switchTemplate.pending}
                  onChange={(event) => switchTemplate.run(event.target.value).catch((error) => toast.push(error.message))}
                >
                  {(templates.data ?? []).map((row) => (
                    <option key={row.key} value={row.key}>
                      模板：{templateLabel(row)}
                    </option>
                  ))}
                </select>
                <button
                  className="btn sm"
                  disabled={refresh.pending}
                  onClick={() => refresh.run().catch((error) => toast.push(error.message))}
                >
                  重新取数
                </button>
                <button
                  className="btn sm"
                  disabled={submit.pending || (check.data && !check.data.ok)}
                  onClick={() => submit.run().catch((error) => toast.push(error.message))}
                >
                  提交审核
                </button>
              </>
            ) : null}
            {version.state === 'review' && can('report.approve') ? (
              <>
                <button className="btn sm" onClick={() => setRejecting(true)}>
                  退回
                </button>
                <button
                  className="btn primary sm"
                  onClick={() =>
                    sign('批准报告', version.id, ['批准报告'], version.row_version)
                      .then((signatureId) =>
                        signatureId ? approve.run({ conclusion: 'approved', signature_id: signatureId }) : undefined,
                      )
                      .catch((error) => toast.push(error.message))
                  }
                >
                  批准
                </button>
              </>
            ) : null}
            {version.state === 'approved' && can('report.publish') ? (
              <button
                className="btn primary sm"
                disabled={publish.pending || (check.data && !check.data.ok)}
                onClick={() =>
                  sign('发布报告', version.id, ['发布报告'], version.row_version)
                    .then((signatureId) => (signatureId ? publish.run(signatureId) : undefined))
                    .catch((error) => toast.push(error.message))
                }
              >
                发布
              </button>
            ) : null}
            {version.pdf_file_id ? (
              <button
                className="btn sm"
                onClick={() =>
                  api
                    .download(`/reports/${version.id}/download`, `${version.code}-v${version.version}.pdf`)
                    .catch((error) => toast.push(error.message))
                }
              >
                下载 PDF
              </button>
            ) : null}
            {version.state === 'published' && can('report.edit') ? (
              <button
                className="btn sm"
                disabled={revise.pending}
                onClick={() => revise.run().catch((error) => toast.push(error.message))}
              >
                基于新结果建新版本
              </button>
            ) : null}
          </div>

          {content ? (
            <>
              <Panel title="结论">
                {version.state === 'draft' && can('report.edit') ? (
                  <>
                    <Field label="结论">
                      <textarea
                        rows={3}
                        defaultValue={content.conclusion}
                        onChange={(event) => setConclusion(event.target.value)}
                      />
                    </Field>
                    <button
                      className="btn sm"
                      disabled={!conclusion || save.pending}
                      onClick={() => save.run().catch((error) => toast.push(error.message))}
                    >
                      保存结论
                    </button>
                  </>
                ) : (
                  <div className="small">{content.conclusion || '—'}</div>
                )}
              </Panel>

              {content.batches ? <BatchesSection section={content.batches} /> : null}

              <Panel title="统计（正式范围）" flush>
                {content.statistics.length ? (
                  <table>
                    <thead>
                      <tr>
                        <th>指标</th>
                        <th>纳入</th>
                        <th>排除</th>
                        <th>均值</th>
                        <th>SD</th>
                        <th>CV%</th>
                      </tr>
                    </thead>
                    <tbody>
                      {content.statistics.map((row, index) => (
                        <tr key={index}>
                          <td>{String(row.metric_name)}</td>
                          <td className="mono">{String(row.included)}</td>
                          <td className="mono">{String(row.excluded)}</td>
                          <td className="mono">{String(row.mean)}</td>
                          <td className="mono">{String(row.sd)}</td>
                          <td className="mono">{String(row.cv_pct)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <Empty>没有可纳入正式统计的数值结果</Empty>
                )}
              </Panel>

              <Panel title="排除说明" flush>
                {content.exclusions.length ? (
                  <table>
                    <thead>
                      <tr>
                        <th>样本</th>
                        <th>指标</th>
                        <th>版本</th>
                        <th>排除原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {content.exclusions.map((row, index) => (
                        <tr key={index}>
                          <td className="mono small">{String(row.assignment_id)}</td>
                          <td className="small">{String(row.metric_name)}</td>
                          <td className="small">v{String(row.result_version)}</td>
                          <td className="small">{String(row.reason_label)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <Empty>没有被排除的记录</Empty>
                )}
              </Panel>

              <Panel title="人员、设备与物料" flush>
                <table>
                  <tbody>
                    <tr>
                      <td className="small muted">负责人 / 执行人 / 复核人</td>
                      <td className="small">
                        {content.resources.owner || '—'} / {content.resources.assignee || '—'} /{' '}
                        {content.resources.reviewer || '—'}
                      </td>
                    </tr>
                    <tr>
                      <td className="small muted">设备</td>
                      <td className="small mono">{content.resources.stations.join('、') || '—'}</td>
                    </tr>
                    <tr>
                      <td className="small muted">SOP</td>
                      <td className="small">
                        {content.method_section.sop || '—'} {content.method_section.sop_version || ''}
                        {content.method_section.sop_checksum ? (
                          <span className="tiny muted mono"> · {content.method_section.sop_checksum.slice(0, 12)}…</span>
                        ) : null}
                      </td>
                    </tr>
                  </tbody>
                </table>
                {content.resources.materials.length ? (
                  <table>
                    <thead>
                      <tr>
                        <th>批号</th>
                        <th>物料</th>
                        <th>授权预留</th>
                        <th>实际消耗</th>
                        <th>损耗</th>
                      </tr>
                    </thead>
                    <tbody>
                      {content.resources.materials.map((row, index) => (
                        <tr key={index}>
                          <td className="mono small">{row.lot_id}</td>
                          <td className="small">{row.material}</td>
                          <td className="mono small">{row.qty}</td>
                          <td className="mono small">{row.consumed}</td>
                          <td className="mono small">{row.loss}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="empty">本方法无物料需求</div>
                )}
              </Panel>

              <ReportExtras content={content} />
              <CommentsPanel targetType="report_version" targetId={version.id} />

              {version.publish_snapshot ? (
                <Panel title="发布快照">
                  <div className="small mono">
                    {Object.entries(version.publish_snapshot)
                      .filter(([key]) => key !== 'result_versions')
                      .map(([key, value]) => `${key}=${String(value)}`)
                      .join('  ·  ')}
                  </div>
                  <div className="tiny muted">
                    已固化 {(version.publish_snapshot.result_versions as string[] | undefined)?.length ?? 0} 条结果版本；
                    之后源结果被修订不会改写这份报告。
                  </div>
                </Panel>
              ) : null}
            </>
          ) : null}
        </>
      ) : (
        <ListState loading={detail.loading} error={detail.error} />
      )}

      {rejecting ? (
        <ConfirmDialog
          title="退回报告"
          confirmLabel="退回"
          reasonLabel="退回理由"
          pending={approve.pending}
          error={approve.error?.message}
          onConfirm={(reason) => approve.run({ conclusion: 'rejected', reason }).catch(() => undefined)}
          onClose={() => setRejecting(false)}
        >
          <div className="note">退回后回到草稿，修改记录可追溯。</div>
        </ConfirmDialog>
      ) : null}
    </Modal>
  );
}

/** 多批合并报告的「分批情况」：各批样本数与状态、按样本的进度、放弃记录、按批统计与批次差异。 */
function BatchesSection({ section }: { section: NonNullable<ReportContent['batches']> }) {
  const progress = section.progress;
  return (
    <Panel title="分批情况" flush>
      <table>
        <thead>
          <tr>
            <th>批次</th>
            <th>子任务</th>
            <th>份额</th>
            <th className="num">样本数</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {section.rows.map((row) => (
            <tr key={row.batch_id}>
              <td className="mono small">{row.batch_id}</td>
              <td className="mono small">{row.task_id}</td>
              <td className="small">{row.purpose === 'retest' ? '补测 ' : ''}{row.portion_label || '—'}</td>
              <td className="num">{row.samples}</td>
              <td><Pill state={row.state} label={row.state_label ?? BATCH_STATE_LABEL[row.state] ?? row.state} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="panel-body stack">
        <div className="small">
          计划 {progress.target} 个样本：有效完成 {progress.valid}，失败 {progress.failed}，补测 {progress.retest}，
          签名放弃 {progress.accepted}，短缺 {progress.shortfall}
        </div>
        {section.decisions.map((row, index) => (
          <div key={index} className="tiny muted">
            {clock(row.at)} {row.user} 签名放弃 {row.count} 个：{row.reason}
          </div>
        ))}
        {section.metrics.map((block) => (
          <div key={block.metric_name} className="stack">
            <div className="small"><b>{block.metric_name}</b>（{block.unit}）按批</div>
            {!block.comparable ? <div className="note warn">{block.comparable_reason}：不合并统计</div> : null}
            <table>
              <thead>
                <tr>
                  <th>批次</th>
                  <th className="num">纳入</th>
                  <th className="num">排除</th>
                  <th className="num">均值</th>
                  <th className="num">SD</th>
                  <th className="num">CV%</th>
                </tr>
              </thead>
              <tbody>
                {block.by_batch.map((row) => (
                  <tr key={row.batch_id}>
                    <td className="mono small">{row.batch_id}</td>
                    <td className="num">{row.n_included}</td>
                    <td className="num">{row.n_excluded}</td>
                    <td className="num">{row.mean || '—'}</td>
                    <td className="num">{row.sd || '—'}</td>
                    <td className="num">{row.cv_pct || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {block.batch_effect ? (
              <div className={`tiny ${block.batch_effect.significant ? 'warn-text' : 'muted'}`}>
                批次差异：F({block.batch_effect.df1}, {block.batch_effect.df2}) = {block.batch_effect.f || '—'}；{block.batch_effect.note}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </Panel>
  );
}

function CreateDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const batches = useQuery<BatchSummary[]>('batches', () => api.get<BatchSummary[]>('/batches'));
  const parents = useQuery<Paged<TaskRow>>('tasks:all', () => api.get<Paged<TaskRow>>('/experiment-tasks?page_size=200'));
  // 一个方案分多批执行时在父任务上出一份合并报告；否则按单个批次出
  const [scope, setScope] = useState<'batch' | 'task'>('batch');
  const [batchId, setBatchId] = useState('');
  const [taskId, setTaskId] = useState('');
  const [conclusion, setConclusion] = useState('');
  const [template, setTemplate] = useState('standard');
  const templates = useQuery<ReportTemplate[]>('reports:templates', () => api.get<ReportTemplate[]>('/reports/templates'));
  const chosen = templates.data?.find((row) => row.key === template);

  const create = useMutation(
    () =>
      api.post(
        '/reports',
        scope === 'task' ? { task_id: taskId, conclusion, template } : { batch_id: batchId, conclusion, template },
        true,
      ),
    {
      invalidates: ['reports', 'tasks'],
      onSuccess: () => {
        toast.push('报告草稿已生成');
        onClose();
      },
    },
  );

  const done = (batches.data ?? []).filter((row) => row.state === 'done');
  const splitTasks = (parents.data?.items ?? []).filter((row) => row.children.length && row.state !== 'cancelled');

  return (
    <Modal
      title="生成报告"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={(scope === 'task' ? !taskId : !batchId) || create.pending}
            onClick={() => create.run().catch(() => undefined)}
          >
            生成草稿
          </button>
        </>
      }
    >
      <div className="note">
        取数只有一套：只纳入审核通过且质量有效的结果；模板只决定包含哪些章节、按什么顺序。
      </div>
      <Field label="模板" hint={chosen ? `${chosen.description}` : undefined}>
        <select value={template} onChange={(event) => setTemplate(event.target.value)}>
          {(templates.data ?? []).map((row) => (
            <option key={row.key} value={row.key}>
              {templateLabel(row)}
            </option>
          ))}
        </select>
      </Field>
      {chosen ? <div className="tiny muted">章节：{chosen.sections.map((row) => row.title).join(' → ')}</div> : null}
      <div className="row">
        <label className="check">
          <input type="radio" name="report-scope" checked={scope === 'batch'} onChange={() => setScope('batch')} />
          单个批次
        </label>
        <label className="check" title="一个方案分多批执行：合并统计、分批明细与批次差异，一份报告覆盖全部批次">
          <input type="radio" name="report-scope" checked={scope === 'task'} onChange={() => setScope('task')} />
          已拆分的父任务（合并报告）
        </label>
      </div>
      {scope === 'task' ? (
        <Field label="父任务" hint="批次都跑完、短缺已补测或签名放弃、各批可比时才能出合并报告">
          <select value={taskId} onChange={(event) => setTaskId(event.target.value)}>
            <option value="">选择父任务</option>
            {splitTasks.map((row) => (
              <option key={row.id} value={row.id}>
                {row.id} · {row.title}（{row.children.length} 个子任务，{row.state_label}）
              </option>
            ))}
          </select>
        </Field>
      ) : (
        <Field label="执行批次">
          <select value={batchId} onChange={(event) => setBatchId(event.target.value)}>
            <option value="">选择已完成的批次</option>
            {done.map((row) => (
              <option key={row.id} value={row.id}>
                {row.id} · {row.recipe_name}
              </option>
            ))}
          </select>
        </Field>
      )}
      <Field label="结论草稿">
        <textarea rows={3} value={conclusion} onChange={(event) => setConclusion(event.target.value)} />
      </Field>
      {create.error ? (
        <div className="note bad">
          {create.error.message}
          <Blocked reasons={create.error.blocked.map((row) => row.label)} />
        </div>
      ) : null}
    </Modal>
  );
}

/** 模板 2.0 起的补充章节：仪器与设备方法、原始数据文件、数据质量标记、操作记录。 */
/* 内置模板版本号是「2.1」这种；组织模板按整数版本递增，标成「v3 · 本组织」以便区分。 */
function templateLabel(row: ReportTemplate) {
  return row.builtin === false ? `${row.name} v${row.version} · 本组织` : `${row.name} ${row.version}`;
}

function ReportExtras({ content }: { content: ReportContent }) {
  const sections = content.template?.sections;
  const show = (key: string) => !sections || sections.includes(key);
  const texts = Object.entries(content.template?.texts ?? {});
  return (
    <>
      {texts.length ? (
        // 组织报告模板里的固定文字章节（声明、方法说明）：PDF 按模板顺序插在对应位置，这里集中列出
        <Panel title="模板文字章节">
          {texts.map(([key, row]) => (
            <div key={key} style={{ marginBottom: 8 }}>
              <b className="small">{row.title}</b>
              <div className="small" style={{ whiteSpace: 'pre-wrap' }}>{row.body}</div>
            </div>
          ))}
        </Panel>
      ) : null}
      {show('instruments') && content.instruments ? (
        <Panel title="仪器与设备方法" flush>
          {content.instruments.length ? (
            <table>
              <thead>
                <tr>
                  <th>工位</th>
                  <th>型号 / 厂商</th>
                  <th>序列号 / 固件</th>
                  <th>校准</th>
                  <th>设备方法</th>
                </tr>
              </thead>
              <tbody>
                {content.instruments.map((row) => (
                  <tr key={row.station_id}>
                    <td className="small">
                      <b className="mono">{row.station_id}</b> {row.name}
                      <div className="tiny muted">{row.kind} · {row.driver || '—'}</div>
                    </td>
                    <td className="small">{row.model || '—'} / {row.vendor || '—'}</td>
                    <td className="small mono">{row.serial || '—'} / {row.firmware || '—'}</td>
                    <td className="small">{row.calibration}</td>
                    <td className="small">{row.methods.join('、') || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty">没有使用设备工位</div>
          )}
        </Panel>
      ) : null}
      {show('raw_files') && content.raw_files ? (
        <Panel title={`原始数据文件（${content.raw_files.length}）`} flush>
          {content.raw_files.length ? (
            <table>
              <tbody>
                {content.raw_files.map((row) => (
                  <tr key={row.id}>
                    <td className="small">{row.filename}</td>
                    <td className="small">{row.usage.join('、')}</td>
                    <td className="small mono">{row.size} B</td>
                    <td className="tiny mono muted">{row.checksum.slice(0, 16)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty">没有关联的原始数据文件</div>
          )}
        </Panel>
      ) : null}
      {show('data_flags') && content.data_flags?.length ? (
        <Panel title={`数据质量标记（${content.data_flags.length}）`} flush>
          <table>
            <tbody>
              {content.data_flags.map((row, index) => (
                <tr key={index}>
                  <td className="small">{row.scope}</td>
                  <td className="small mono">{row.target}</td>
                  <td className="small warn-text">{row.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      ) : null}
      {show('operation_log') && content.operation_log ? (
        <Panel title={`操作记录（${content.operation_log.length}）`} flush>
          <div style={{ maxHeight: 280, overflow: 'auto' }}>
            <table>
              <tbody>
                {content.operation_log.map((row, index) => (
                  <tr key={index}>
                    <td className="tiny mono">{row.time.replace('T', ' ').slice(5, 16)}</td>
                    <td className="small">{row.user}</td>
                    <td className="small">
                      {row.action}
                      {row.signed ? <span className="tag">签名</span> : null}
                    </td>
                    <td className="tiny muted">{row.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : null}
    </>
  );
}
