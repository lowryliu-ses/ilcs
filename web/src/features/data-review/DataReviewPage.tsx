import { useState } from 'react';

import { api, pageQuery } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import { FlagList } from '../../shared/flags';
import type {
  AnalysisTaskRow, MetricRow, Paged, ResultValueRow, SampleRow,
} from '../../shared/types';
import {
  Blocked, ConfirmDialog, Empty, Field, ListState, Modal, NumberInput, Pager, Panel, Pill, useToast,
} from '../../shared/ui';

const REVIEW_STATES: [string, string][] = [
  ['pending', '待复核'],
  ['approved', '已通过'],
  ['rejected', '已退回'],
];
const QUALITIES: [string, string][] = [
  ['valid', '有效'],
  ['suspect', '可疑'],
  ['invalid', '无效'],
];
/** 来源标签。迁移来的历史数据要如实标出来，不能混在「人工录入」里。 */
const PROVENANCE_LABEL: Record<string, string> = {
  device: '设备回传',
  manual: '人工录入',
  correction: '人工更正',
  legacy_unreviewed: '历史迁移',
};

export function DataReviewPage() {
  const { can } = useSession();
  const [page, setPage] = useState(1);
  const [reviewState, setReviewState] = useState('pending');
  const [quality, setQuality] = useState('');
  const [reviewing, setReviewing] = useState<ResultValueRow | null>(null);
  const [revising, setRevising] = useState<ResultValueRow | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [creatingTask, setCreatingTask] = useState(false);
  const [entering, setEntering] = useState<AnalysisTaskRow | null>(null);
  const [retesting, setRetesting] = useState<AnalysisTaskRow | null>(null);
  const [taskPage, setTaskPage] = useState(1);

  const query = pageQuery({ page, page_size: 20, review_state: reviewState, quality });
  const values = useQuery<Paged<ResultValueRow>>(
    `results:values:${query}`, () => api.get<Paged<ResultValueRow>>(`/result-values${query}`), 20000,
  );

  const taskQuery = pageQuery({ page: taskPage, page_size: 10 });
  const tasks = useQuery<Paged<AnalysisTaskRow>>(
    `results:tasks:${taskQuery}`,
    () => api.get<Paged<AnalysisTaskRow>>(`/analysis-tasks${taskQuery}`),
    20000,
  );

  const rows = values.data?.items ?? [];
  const taskRows = tasks.data?.items ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>数据审核</h1>
        <span className="small muted">
          采集完成、质量有效、审核通过是三件事。正式统计要求三者同时成立；缺测不是 0，声明测不到要写原因。
        </span>
      </div>

      <Panel
        title={`结果明细（${values.data?.total ?? 0}）`}
        aside={
          <div className="filters">
            <select value={reviewState} onChange={(event) => { setReviewState(event.target.value); setPage(1); }}>
              <option value="">全部审核状态</option>
              {REVIEW_STATES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <select value={quality} onChange={(event) => { setQuality(event.target.value); setPage(1); }}>
              <option value="">全部质量</option>
              <option value="unassessed">未判定</option>
              {QUALITIES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
        }
        flush
      >
        <ListState
          loading={values.loading && !values.data}
          error={values.error}
          empty={!rows.length}
          emptyText="没有符合条件的结果明细"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>指标</th>
                <th>数值</th>
                <th>样本 / 任务</th>
                <th>版本</th>
                <th>质量</th>
                <th>审核</th>
                <th>来源</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <b>{row.metric_name}</b>
                    <div className="tiny muted mono">{row.metric_code}</div>
                  </td>
                  <td className="mono">
                    {row.not_measured_reason ? (
                      <>
                        <span className="tag warn">未测</span>
                        <div className="tiny muted">{row.not_measured_reason}</div>
                      </>
                    ) : (
                      row.display
                    )}
                  </td>
                  <td className="small mono">
                    {row.assignment_id || row.physical_sample_id}
                    <div className="tiny muted">任务 {row.analysis_task_id.slice(0, 8)}</div>
                  </td>
                  <td className="small">
                    v{row.result_version}
                    {row.revises_id ? <div className="tiny muted">更正自上一版本</div> : null}
                    {row.superseded_by_id ? <div className="tiny warn-text">已被取代</div> : null}
                  </td>
                  <td>
                    <Pill state={row.quality} label={row.quality_label} />
                    <FlagList flags={row.flags} />
                  </td>
                  <td>
                    <Pill state={row.review_state} label={row.review_label} />
                    {row.official ? <div className="tiny">纳入正式统计</div> : (
                      <div className="tiny muted">不纳入正式统计</div>
                    )}
                  </td>
                  <td className="small">
                    {PROVENANCE_LABEL[row.provenance] ?? row.provenance}
                    {row.entered_by_name ? <div className="tiny muted">{row.entered_by_name}</div> : null}
                    {row.station_id || row.instrument ? (
                      <div className="tiny muted mono">{[row.station_id, row.instrument].filter(Boolean).join(' · ')}</div>
                    ) : null}
                    {row.provenance === 'legacy_unreviewed' ? (
                      <div className="tiny warn-text">历史质量标记不等于审核</div>
                    ) : null}
                  </td>
                  <td className="row-end">
                    <button className="btn sm" onClick={() => setTaskId(row.analysis_task_id)}>
                      原始
                    </button>
                    {can('result.enter') && !row.superseded_by_id ? (
                      <button className="btn sm" onClick={() => setRevising(row)}>
                        更正
                      </button>
                    ) : null}
                    {can('result.review') && row.review_state === 'pending' && !row.superseded_by_id ? (
                      <button className="btn sm primary" onClick={() => setReviewing(row)}>
                        复核
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <Pager
          page={values.data?.page ?? 1}
          pageSize={values.data?.page_size ?? 20}
          total={values.data?.total ?? 0}
          onChange={setPage}
        />
      </Panel>

      <Panel
        title={`检测任务（${tasks.data?.total ?? 0}）`}
        aside={
          can('analysis.create') ? (
            <button className="btn primary sm" onClick={() => setCreatingTask(true)}>
              建检测任务
            </button>
          ) : null
        }
        flush
      >
        <div className="note">
          建任务时冻结「要求测哪些指标」。只有这些指标的结果才会被接收入账；
          补测别的指标要另建任务，不是往这条任务里加。
        </div>
        <ListState
          loading={tasks.loading && !tasks.data}
          error={tasks.error}
          empty={!taskRows.length}
          emptyText="还没有检测任务。批次跑完后在这里给样本建检测任务，结果才有地方入账。"
        />
        {taskRows.length ? (
          <table>
            <thead>
              <tr>
                <th>样本</th>
                <th>检测方法</th>
                <th>轮次</th>
                <th>要求指标</th>
                <th>待复核</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {taskRows.map((row) => (
                <tr key={row.id}>
                  <td className="small mono">
                    {row.sample_id || row.physical_sample_id}
                    <div className="tiny muted">任务 {row.id.slice(0, 8)}</div>
                  </td>
                  <td className="small">
                    {row.method || '—'}
                    <div className="tiny muted">{row.method_version || '未记检测方法版本'}</div>
                  </td>
                  <td className="mono small">
                    第 {row.round_no} 轮
                    {row.retest_of ? <div className="tiny muted">重测</div> : null}
                  </td>
                  <td className="small">
                    {row.required_metrics.filter((metric) => metric.collected).length}/
                    {row.required_metrics.length}
                    {row.missing_metrics.length ? (
                      <div className="tiny warn-text">缺 {row.missing_metrics.join('、')}</div>
                    ) : null}
                  </td>
                  <td className="mono small">{row.review_pending}</td>
                  <td>
                    <Pill state={row.state} label={row.state_label} />
                  </td>
                  <td className="row-end">
                    {can('result.enter') && !['collected', 'cancelled'].includes(row.state) ? (
                      <button className="btn sm" onClick={() => setEntering(row)}>
                        录入结果
                      </button>
                    ) : null}
                    {can('analysis.create') && row.state !== 'cancelled' ? (
                      <button className="btn sm" onClick={() => setRetesting(row)}>
                        重测
                      </button>
                    ) : null}
                    <button className="btn sm" onClick={() => setTaskId(row.id)}>
                      详情
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <Pager
          page={tasks.data?.page ?? 1}
          pageSize={tasks.data?.page_size ?? 10}
          total={tasks.data?.total ?? 0}
          onChange={setTaskPage}
        />
      </Panel>

      {reviewing ? <ReviewDialog value={reviewing} onClose={() => setReviewing(null)} /> : null}
      {revising ? <ReviseDialog value={revising} onClose={() => setRevising(null)} /> : null}
      {taskId ? <TaskDialog taskId={taskId} onClose={() => setTaskId(null)} /> : null}
      {creatingTask ? <CreateTaskDialog onClose={() => setCreatingTask(false)} /> : null}
      {entering ? <ManualEntryDialog task={entering} onClose={() => setEntering(null)} /> : null}
      {retesting ? <RetestDialog task={retesting} onClose={() => setRetesting(null)} /> : null}
    </div>
  );
}

/* 指标多选。要求指标在建任务时冻结，所以这里挑的是「这次要测什么」，
   不是「以后可能测什么」——冻结之后回传只认这些。 */
function MetricPicker({
  chosen,
  onChange,
}: {
  chosen: string[];
  onChange: (next: string[]) => void;
}) {
  const metrics = useQuery<MetricRow[]>('metrics:active', () =>
    api.get<MetricRow[]>('/metrics?only_active=true'),
  );
  const rows = metrics.data ?? [];

  if (metrics.error) return <div className="note bad">指标清单读取失败：{metrics.error.message}</div>;
  if (!rows.length) {
    return (
      <div className="note bad">
        还没有在用的指标定义。请先到「数据与报告 · 指标与规则」登记指标，检测任务才有可要求的指标。
      </div>
    );
  }
  return (
    <div className="chips">
      {rows.map((row) => (
        <label key={row.id} className={`chip${chosen.includes(row.id) ? ' on' : ''}`}>
          <input
            type="checkbox"
            checked={chosen.includes(row.id)}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...chosen, row.id]
                  : chosen.filter((item) => item !== row.id),
              )
            }
          />
          {row.name} {row.version}
          {row.unit ? ` (${row.unit})` : ''}
        </label>
      ))}
    </div>
  );
}

function CreateTaskDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const samples = useQuery<Paged<SampleRow>>('samples:for-analysis', () =>
    api.get<Paged<SampleRow>>('/samples?page_size=200'),
  );
  const [sampleId, setSampleId] = useState('');
  const [method, setMethod] = useState('');
  const [methodVersion, setMethodVersion] = useState('');
  const [chosen, setChosen] = useState<string[]>([]);

  const create = useMutation(
    () =>
      api.post('/analysis-tasks', {
        physical_sample_id: sampleId,
        method,
        method_version: methodVersion,
        required_metrics: chosen,
      }),
    {
      invalidates: ['results', 'dashboard'],
      onSuccess: () => {
        toast.push('检测任务已建立，要求指标已冻结');
        onClose();
      },
    },
  );

  const sampleRows = samples.data?.items ?? [];

  return (
    <Modal
      title="建检测任务"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!sampleId || !chosen.length || create.pending}
            onClick={() => create.run().catch(() => undefined)}
          >
            建立
          </button>
        </>
      }
    >
      <Field label="样本" hint="批次生成的样本与独立登记的样本都在这里">
        <select value={sampleId} onChange={(event) => setSampleId(event.target.value)}>
          <option value="">请选择</option>
          {sampleRows.map((row) => (
            <option key={row.id} value={row.id}>
              {row.barcode || row.id} · {row.sample_type || '未分类'} · {row.lifecycle_label}
            </option>
          ))}
        </select>
      </Field>
      <div className="grid cols-2">
        <Field label="检测方法">
          <input value={method} onChange={(event) => setMethod(event.target.value)} />
        </Field>
        <Field label="方法版本" hint="留档用：同一指标在不同方法下不可直接比较">
          <input value={methodVersion} onChange={(event) => setMethodVersion(event.target.value)} />
        </Field>
      </div>
      <Field label="要求指标（建后冻结）">
        <MetricPicker chosen={chosen} onChange={setChosen} />
      </Field>
      {create.error ? <div className="note bad">{create.error.message}</div> : null}
    </Modal>
  );
}

/* 人工录入。没有对接 LIMS 的仪器靠它入账，所以它必须在界面上有——
   但录入人之后不能审核自己录的这条，那条规则在服务端。 */
function ManualEntryDialog({ task, onClose }: { task: AnalysisTaskRow; onClose: () => void }) {
  const toast = useToast();
  // 每个任务一个稳定事件号：重试同一次录入不会记成两条
  const [eventId] = useState(
    () => `manual-${task.id.slice(0, 8)}-${Date.now().toString(36)}`,
  );
  const [entries, setEntries] = useState<Record<string, { value: string; missing: string }>>(() =>
    Object.fromEntries(task.required_metrics.map((row) => [row.id, { value: '', missing: '' }])),
  );

  const enter = useMutation(
    () =>
      api.post(`/analysis-tasks/${task.id}/results`, {
        event_id: eventId,
        metrics: task.required_metrics
          .filter((row) => entries[row.id]?.value !== '' || entries[row.id]?.missing)
          .map((row) => ({
            metric_version_id: row.id,
            value: entries[row.id].missing ? null : entries[row.id].value,
            unit: row.unit,
            not_measured_reason: entries[row.id].missing,
          })),
      }),
    {
      invalidates: ['results', 'dashboard'],
      onSuccess: () => {
        toast.push('已录入，状态为待复核');
        onClose();
      },
    },
  );

  const filled = task.required_metrics.filter(
    (row) => entries[row.id]?.value !== '' || entries[row.id]?.missing,
  ).length;

  return (
    <Modal
      title={`人工录入 · 任务 ${task.id.slice(0, 8)}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!filled || enter.pending}
            onClick={() => enter.run().catch(() => undefined)}
          >
            录入
          </button>
        </>
      }
    >
      <div className="note">
        录入后状态是「待复核」，不是「有效」。你录的这条自己不能审核——换人复核才算过。
        测不到的指标请写原因，不要填 0：缺测和 0 是两件事。
      </div>
      <table>
        <thead>
          <tr>
            <th>指标</th>
            <th>数值</th>
            <th>测不到的原因</th>
          </tr>
        </thead>
        <tbody>
          {task.required_metrics.map((row) => (
            <tr key={row.id}>
              <td className="small">
                {row.name}
                <div className="tiny muted mono">{row.unit || '无单位'}</div>
                {row.collected ? <div className="tiny muted">已有记录</div> : null}
              </td>
              <td>
                <input
                  value={entries[row.id]?.value ?? ''}
                  disabled={Boolean(entries[row.id]?.missing)}
                  onChange={(event) =>
                    setEntries({
                      ...entries,
                      [row.id]: { value: event.target.value, missing: '' },
                    })
                  }
                />
              </td>
              <td>
                <input
                  value={entries[row.id]?.missing ?? ''}
                  placeholder="如：样本量不足"
                  onChange={(event) =>
                    setEntries({
                      ...entries,
                      [row.id]: { value: '', missing: event.target.value },
                    })
                  }
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {enter.error ? <div className="note bad">{enter.error.message}</div> : null}
    </Modal>
  );
}

/* 重测产生新任务 + 新轮次，原任务与原结果一条不动——否则「这个数当初是多少」就查不回来了。 */
function RetestDialog({ task, onClose }: { task: AnalysisTaskRow; onClose: () => void }) {
  const toast = useToast();
  const [reason, setReason] = useState('');
  const [chosen, setChosen] = useState<string[]>(task.required_metrics.map((row) => row.id));

  const retest = useMutation(
    () =>
      api.post(`/analysis-tasks/${task.id}/retests`, {
        reason,
        method: task.method,
        method_version: task.method_version,
        required_metrics: chosen,
      }),
    {
      invalidates: ['results', 'dashboard'],
      onSuccess: (created) => {
        toast.push(`已建第 ${(created as AnalysisTaskRow).round_no} 轮重测任务`);
        onClose();
      },
    },
  );

  return (
    <Modal
      title={`重测 · 任务 ${task.id.slice(0, 8)}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!reason.trim() || !chosen.length || retest.pending}
            onClick={() => retest.run().catch(() => undefined)}
          >
            建重测任务
          </button>
        </>
      }
    >
      <div className="note">
        重测建新任务、新轮次。原任务与已入账的结果保持原样，两轮数据都能查到，
        统计按结果版本取用。
      </div>
      <Field label="重测原因（必填）">
        <textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      <Field label="这一轮要测的指标">
        <MetricPicker chosen={chosen} onChange={setChosen} />
      </Field>
      {retest.error ? <div className="note bad">{retest.error.message}</div> : null}
    </Modal>
  );
}

function ReviewDialog({ value, onClose }: { value: ResultValueRow; onClose: () => void }) {
  const toast = useToast();
  const { sign } = useSignature();
  const [conclusion, setConclusion] = useState<'approved' | 'rejected'>('approved');
  const [quality, setQuality] = useState('valid');
  const [reason, setReason] = useState('');

  const review = useMutation(
    (signatureId: string) =>
      api.post(
        `/result-values/${value.id}/review`,
        {
          conclusion,
          quality: conclusion === 'approved' ? quality : 'unassessed',
          reason,
          result_version: value.result_version,
          signature_id: signatureId,
        },
        true,
      ),
    {
      invalidates: ['results', 'dashboard'],
      onSuccess: () => {
        toast.push('复核已记录');
        onClose();
      },
    },
  );

  const needsReason = conclusion === 'rejected' || quality === 'suspect' || quality === 'invalid';

  return (
    <Modal
      title={`复核 · ${value.metric_name} v${value.result_version}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={review.pending || (needsReason && !reason.trim())}
            onClick={() =>
              sign(
                conclusion === 'approved' ? '数据复核通过' : '数据复核退回',
                value.id,
                conclusion === 'approved'
                  ? ['数据复核通过', '质量判定']
                  : ['数据复核退回'],
                value.result_version,
              )
                .then((signatureId) => (signatureId ? review.run(signatureId) : undefined))
                .catch((error) => toast.push(error.message))
            }
          >
            签署并提交
          </button>
        </>
      }
    >
      <div className="note">
        当前值 <b className="mono">{value.display}</b>
        {value.not_measured_reason ? `（声明未测：${value.not_measured_reason}）` : ''}；
        录入来源 {value.provenance === 'device' ? '设备回传' : value.entered_by_name || '人工'}。
        <div className="small muted">
          审核通过必须同时给出质量判定：审核完成只表示流程走完，不代表数据可用。
        </div>
      </div>
      <Field label="审核结论">
        <select value={conclusion} onChange={(event) => setConclusion(event.target.value as typeof conclusion)}>
          <option value="approved">通过</option>
          <option value="rejected">退回</option>
        </select>
      </Field>
      {conclusion === 'approved' ? (
        <Field label="质量判定" hint="判为可疑或无效时必须写理由；它们不会进入正式统计">
          <select value={quality} onChange={(event) => setQuality(event.target.value)}>
            {QUALITIES.map(([code, label]) => (
              <option key={code} value={code}>
                {label}
              </option>
            ))}
          </select>
        </Field>
      ) : null}
      <Field label={needsReason ? '理由（必填）' : '理由'}>
        <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {review.error ? (
        <div className="note bad">
          {review.error.message}
          <Blocked reasons={review.error.blocked.map((row) => row.label)} />
        </div>
      ) : null}
    </Modal>
  );
}

function ReviseDialog({ value, onClose }: { value: ResultValueRow; onClose: () => void }) {
  const toast = useToast();
  const [numeric, setNumeric] = useState<number | ''>(
    typeof value.value === 'number' ? value.value : '',
  );
  const [text, setText] = useState(typeof value.value === 'string' ? value.value : '');
  const [reason, setReason] = useState('');
  const [notMeasured, setNotMeasured] = useState('');

  const revise = useMutation(
    () =>
      api.post(
        `/result-values/${value.id}/revisions`,
        {
          value: notMeasured ? null : value.value_type === 'number' ? numeric : text,
          unit: value.unit,
          not_measured_reason: notMeasured,
          reason,
        },
        true,
      ),
    {
      invalidates: ['results', 'dashboard'],
      onSuccess: () => {
        toast.push('已生成新版本，原记录保留');
        onClose();
      },
    },
  );

  return (
    <Modal
      title={`更正 · ${value.metric_name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!reason.trim() || revise.pending}
            onClick={() => revise.run().catch(() => undefined)}
          >
            生成新版本
          </button>
        </>
      }
    >
      <div className="note">
        更正会生成新的结果版本并显式引用原版本（v{value.result_version}）。旧记录不会被覆盖，
        已发布的报告也不受影响。
      </div>
      {notMeasured ? null : value.value_type === 'number' ? (
        <Field label={`新值（${value.unit}）`}>
          <NumberInput value={numeric} onChange={setNumeric} />
        </Field>
      ) : (
        <Field label="新值">
          <input value={text} onChange={(event) => setText(event.target.value)} />
        </Field>
      )}
      <Field label="声明无法测得" hint="填了原因就按未测记录，不写 0">
        <input value={notMeasured} onChange={(event) => setNotMeasured(event.target.value)} />
      </Field>
      <Field label="更正原因（必填）">
        <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {revise.error ? <div className="note bad">{revise.error.message}</div> : null}
    </Modal>
  );
}

function TaskDialog({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const toast = useToast();
  const { can } = useSession();
  const [cancelling, setCancelling] = useState(false);
  const detail = useQuery<AnalysisTaskRow>(
    `results:task:${taskId}`, () => api.get<AnalysisTaskRow>(`/analysis-tasks/${taskId}`),
  );
  const task = detail.data;

  /* 取消只对还没采完的任务开放：采集完成的任务里已经有数据，
     那些数据的处置走复核与更正，不能靠取消任务把它们一笔带过。 */
  const cancel = useMutation(
    (reason: string) => api.post(`/analysis-tasks/${taskId}/cancel`, { reason }),
    {
      invalidates: ['results', 'dashboard', 'audit'],
      onSuccess: () => {
        toast.push('检测任务已取消');
        setCancelling(false);
        onClose();
      },
    },
  );

  return (
    <Modal title={`检测任务 · ${taskId.slice(0, 8)}`} onClose={onClose} wide>
      {cancelling ? (
        <ConfirmDialog
          title="取消检测任务"
          danger
          confirmLabel="取消任务"
          reasonLabel="取消理由"
          pending={cancel.pending}
          error={cancel.error?.message}
          onConfirm={(reason) => cancel.run(reason)}
          onClose={() => setCancelling(false)}
        >
          已入账的结果不会被删除，它们仍然按原样留档待复核。样本如需重测，请另建检测任务。
        </ConfirmDialog>
      ) : null}
      {task ? (
        <>
          <div className="metrics">
            <div className="metric">
              <span className="metric-label">采集状态</span>
              <strong className="metric-value">
                <Pill state={task.state} label={task.state_label} />
              </strong>
              <span className="metric-hint">
                {task.collected ? '全部要求指标已有合法记录' : `还缺 ${task.missing_metrics.length} 项`}
              </span>
            </div>
            <div className="metric">
              <span className="metric-label">检测轮次</span>
              <strong className="metric-value">第 {task.round_no} 轮</strong>
              <span className="metric-hint">{task.retest_of ? `重测自 ${task.retest_of.slice(0, 8)}` : '首轮'}</span>
            </div>
            <div className="metric">
              <span className="metric-label">待复核</span>
              <strong className="metric-value">{task.review_pending}</strong>
              <span className="metric-hint">采集完成不等于审核通过</span>
            </div>
          </div>

          {can('analysis.create') && task.state !== 'collected' && task.state !== 'cancelled' ? (
            <div className="panel-aside" style={{ justifyContent: 'flex-end', margin: '10px 0' }}>
              <button className="btn sm danger" onClick={() => setCancelling(true)}>
                取消任务
              </button>
            </div>
          ) : null}

          <Panel title="冻结的要求指标" flush>
            <table>
              <thead>
                <tr>
                  <th>指标</th>
                  <th>单位</th>
                  <th>采集</th>
                </tr>
              </thead>
              <tbody>
                {task.required_metrics.map((row) => (
                  <tr key={row.id}>
                    <td>
                      {row.name}
                      <div className="tiny muted mono">{row.code}</div>
                    </td>
                    <td className="small mono">{row.unit || '—'}</td>
                    <td className="small">
                      {row.not_measured ? (
                        <span className="tag warn">声明未测</span>
                      ) : row.collected ? (
                        <span className="tag">已采集</span>
                      ) : (
                        <span className="tag bad">缺</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          <Panel title="结果版本与原始文件" flush>
            {task.values?.length ? (
              <table>
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>版本</th>
                    <th>数值</th>
                    <th>采集时间</th>
                    <th>解析版本</th>
                    <th>原始文件</th>
                  </tr>
                </thead>
                <tbody>
                  {task.values.map((row) => (
                    <tr key={row.id} className={row.superseded_by_id ? 'muted-row' : undefined}>
                      <td className="small">{row.metric_name}</td>
                      <td className="small">v{row.result_version}</td>
                      <td className="mono small">{row.display}</td>
                      <td className="small">{row.collected_at ? clock(row.collected_at) : '—'}</td>
                      <td className="small mono">{row.parser_version || '—'}</td>
                      <td className="small">
                        {row.raw_file_id ? (
                          <button
                            className="btn sm"
                            onClick={() =>
                              api
                                .download(`/files/${row.raw_file_id}/download`, `${row.id}.raw`)
                                .catch((error) => toast.push(error.message))
                            }
                          >
                            下载
                          </button>
                        ) : row.source_ref ? (
                          <span className="tag warn" title={row.source_ref}>
                            原件缺失
                          </span>
                        ) : (
                          <span className="muted">无</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>还没有结果记录</Empty>
            )}
          </Panel>
        </>
      ) : (
        <ListState loading={detail.loading} error={detail.error} />
      )}
    </Modal>
  );
}
