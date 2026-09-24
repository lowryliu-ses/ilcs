import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api, idempotencyKey } from '../../shared/api';
import { FlagList } from '../../shared/flags';
import { clock, num, params as formatParams, time } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSignature } from '../../shared/signature';
import { useSession } from '../../shared/session';
import type {
  BatchDetail, ExceptionEventRow, LabwareRow, Preflight, RecoveryEvaluation, StepRow, StepRunRow, TelemetryFeed,
} from '../../shared/types';
import { LineChart } from '../../shared/chart';
import { FlowGraph, type FlowGraphEdge, type FlowGraphLoop, type FlowGraphNode } from '../../shared/flowgraph';
import {
  Blocked, CheckList, ConfirmDialog, Empty, Field, GateBanner, Modal, NumberInput, Panel, Pill,
  useToast,
} from '../../shared/ui';

const SIGN_MEANINGS = {
  dispatch: ['批准执行', '复核通过'],
  recover: ['已核实实际量与设备状态', '接受偏差风险', '样品报废'],
  abort: ['安全终止', '样品报废'],
  verify: ['已到现场核实设备实态', '已核对检查点与实际量'],
};

const VERIFY_OPTIONS: { value: 'executed' | 'not_executed' | 'partial'; label: string; hint: string }[] = [
  { value: 'executed', label: '已执行', hint: '按人工核实写检查点；终止指令已执行即现场已安全停机，批次直接终止' },
  { value: 'not_executed', label: '未执行', hint: '设备确认没有动作，该步骤可在恢复评估中续跑或重试' },
  { value: 'partial', label: '部分执行', hint: '物理状态不可复现，续跑与重试都会被禁止，只能终止' },
];

export function BatchDetailPage() {
  const { batchId = '' } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const unschedule = useMutation(() => api.post(`/batches/${batchId}/unschedule`), {
    invalidates: [`batches:${batchId}`, 'batches', 'schedule', 'dashboard', 'audit'],
    onSuccess: () => toast.push('已取消排程，工位时间窗已归还；物料预留保持不变'),
  });
  const removeBatch = useMutation(() => api.remove(`/batches/${batchId}`), {
    invalidates: ['batches', 'schedule', 'dashboard', 'reservations', 'lots', 'audit'],
    onSuccess: () => {
      toast.push('批次已删除，工位时间窗与物料预留已归还');
      navigate('/batches');
    },
  });
  const { sign } = useSignature();
  const { can } = useSession();
  const batch = useQuery<BatchDetail>(`batches:${batchId}`, () => api.get<BatchDetail>(`/batches/${batchId}`), 8000);
  const [dialog, setDialog] = useState<'preflight' | 'hold' | 'recover' | 'abort' | 'delete' | null>(null);
  const [submitting, setSubmitting] = useState<StepRunRow | null>(null);
  const [reviewing, setReviewing] = useState<StepRunRow | null>(null);
  const [viewing, setViewing] = useState<StepRunRow | null>(null);
  const [verifying, setVerifying] = useState<BatchDetail['commands'][number] | null>(null);
  const [gateDeciding, setGateDeciding] = useState<StepRunRow | null>(null);
  const [branchDeciding, setBranchDeciding] = useState<StepRunRow | null>(null);
  const [skipping, setSkipping] = useState<StepRow | null>(null);
  const [signaling, setSignaling] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);

  if (!batch.data) return <div className="boot">{batch.error ? batch.error.message : '加载中…'}</div>;
  const data = batch.data;
  const invalidates = [`batches:${batchId}`, 'batches', 'dashboard', 'schedule', 'alarms', 'audit'];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>
            <span className="mono">{data.id}</span> <Pill state={data.state} label={data.state_label} />
          </h1>
          <div className="small muted">
            {data.recipe_name} · 快照 {data.snapshot.id} v{data.snapshot.version} · 方案{' '}
            <Link to={`/plans/${data.plan_id}`}>{data.plan_id}</Link> v{data.plan_version} · 风险评估{' '}
            {data.snapshot.risk || '缺失'}
            {data.task_id ? (
              <>
                {' '}
                · 任务 <span className="mono">{data.task_id}</span>
              </>
            ) : null}
            {data.sop_snapshot?.code ? (
              <>
                {' '}
                · SOP {data.sop_snapshot.code} {data.sop_snapshot.version}
                <span className="tiny muted mono"> {(data.sop_snapshot.file_checksum ?? '').slice(0, 10)}</span>
              </>
            ) : null}
          </div>
        </div>
        <div className="row">
          {data.state === 'scheduled' ? (
            <button className="btn primary" onClick={() => setDialog('preflight')}>
              开跑检查
            </button>
          ) : null}
          {data.state === 'scheduled' && data.can_control ? (
            <button
              className="btn"
              disabled={unschedule.pending || !data.gate.open}
              title={data.gate.open ? '退回待排程并归还工位时间窗' : '执行门关闭'}
              onClick={() => unschedule.run().catch((error) => toast.push(error.message))}
            >
              取消排程
            </button>
          ) : null}
          {data.state === 'running' ? (
            <button className="btn" onClick={() => setDialog('hold')}>
              请求保持
            </button>
          ) : null}
          {['paused', 'fault'].includes(data.state) ? (
            <button className="btn primary" onClick={() => setDialog('recover')}>
              恢复评估
            </button>
          ) : null}
          {!['done', 'aborted', 'aborting'].includes(data.state) ? (
            <button className="btn danger" onClick={() => setDialog('abort')}>
              终止评估
            </button>
          ) : null}
          {data.can_control && data.delete_blockers.length === 0 ? (
            <button className="btn danger" onClick={() => setDialog('delete')}>
              删除批次
            </button>
          ) : null}
        </div>
      </div>
      <GateBanner gate={data.gate} />

      <div className="note">
        <b>下一步动作：</b>
        {data.next_action.who} · {data.next_action.what}
        <span className="muted">（{data.next_action.why}）</span>
      </div>

      {data.steps.length ? (
        <Panel
          title="流程图"
          aside={<span className="small muted">实线：已走过 · 虚线：未走 / 回环 · 框：子流程</span>}
          flush
        >
          <RunGraph steps={data.steps} selected={focus} onSelect={setFocus} />
          {focus ? (
            <StepActions
              step={data.steps.find((row) => row.step_id === focus) ?? null}
              batchState={data.state}
              onSubmit={setSubmitting}
              onReview={setReviewing}
              onGate={setGateDeciding}
              onBranch={setBranchDeciding}
              onSkip={setSkipping}
              onSignal={setSignaling}
            />
          ) : null}
        </Panel>
      ) : null}

      <Panel title="工步" flush>
        <table>
          <thead>
            <tr>
              <th>步骤</th>
              <th>类型 / 内容</th>
              <th>工位</th>
              <th>计划</th>
              <th>实际</th>
              <th>状态</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.steps.map((step) => (
              <tr key={step.step_id} className={step.index === data.current_step ? 'current' : ''}>
                <td>
                  {step.index + 1}. {step.name}
                  <div className="tiny muted mono">{step.step_id}</div>
                  {step.groups?.length ? (
                    <div className="tiny muted">子流程 · {step.groups.map((group) => group.name).join(' › ')}</div>
                  ) : null}
                  {step.hard?.maxGapMin ? (
                    <div className="tiny warn-text">
                      硬时限：{step.hard.from} 起 {step.hard.maxGapMin} min 内必须开始
                    </div>
                  ) : null}
                </td>
                <td>
                  <span className={`kind ${step.kind}`}>{step.kind_label}</span>{' '}
                  {step.kind === 'device' ? step.cap_name : null}
                  <div className="tiny muted mono">{stepContent(step)}</div>
                </td>
                <td className="mono">
                  {step.needs_station ? step.station_id ?? '—' : <span className="muted">不占工位</span>}
                  {step.transfer_station_id ? <div className="tiny muted">转运 {step.transfer_station_id}</div> : null}
                </td>
                <td className="small mono">
                  {step.needs_station ? `${clock(step.planned_start)} → ${clock(step.planned_end)}` : '—'}
                </td>
                <td className="small mono">
                  {step.actual_end ? time(step.actual_end) : step.run?.ended_at ? time(step.run.ended_at) : '—'}
                  {step.checkpoint_id ? <div className="tiny muted">CP {step.checkpoint_id.slice(0, 8)}</div> : null}
                  {step.attempts.length > 1 ? (
                    <div className="tiny warn-text">共 {step.attempts.length} 次尝试</div>
                  ) : null}
                </td>
                <td>
                  <Pill state={step.state} label={step.run?.state_label ?? stepLabel(step.state)} />
                  {step.run?.reason ? <div className="tiny muted">{step.run.reason}</div> : null}
                  <FlagList flags={step.run?.flags} />
                </td>
                <td className="row-end">
                  {step.run && step.kind === 'manual' && ['ready', 'running'].includes(step.run.state) && can('step.submit') ? (
                    <button className="btn sm primary" onClick={() => setSubmitting(step.run)}>
                      填写记录
                    </button>
                  ) : null}
                  {step.run && step.kind === 'review' && ['ready', 'running'].includes(step.run.state) && can('step.review') ? (
                    <button className="btn sm primary" onClick={() => setReviewing(step.run)}>
                      审核
                    </button>
                  ) : null}
                  {step.run && step.kind === 'gate' && step.run.state === 'ready' && can('step.review') ? (
                    <button className="btn sm primary" onClick={() => setGateDeciding(step.run)}>
                      质检判定
                    </button>
                  ) : null}
                  {step.run && step.kind === 'wait' && step.run.state === 'waiting' ? (
                    step.wait_for?.mode === 'event' ? (
                      can('batch.signal') ? (
                        <button className="btn sm" onClick={() => setSignaling(step.wait_for?.event ?? '')}>
                          发出事件
                        </button>
                      ) : (
                        <span className="tiny muted">等待事件 {step.wait_for?.event}</span>
                      )
                    ) : (
                      <span className="tiny muted">到期 {step.run.due_at ? time(step.run.due_at) : '—'}</span>
                    )
                  ) : null}
                  {step.run && step.kind === 'branch' && step.run.state === 'ready' && (can('step.submit') || can('step.review')) ? (
                    <button className="btn sm primary" onClick={() => setBranchDeciding(step.run)}>
                      选择出口
                    </button>
                  ) : null}
                  {canSkip(step, data.state) && can('batch.recover') ? (
                    <button className="btn sm" onClick={() => setSkipping(step)}>
                      跳过
                    </button>
                  ) : null}
                  {step.run?.form_data?.values ? (
                    <button className="btn sm" onClick={() => setViewing(step.run)}>
                      记录
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="panel-body small muted">
          人工、等待、审核节点不创建设备指令；不占工位的节点也不参与工位冲突检查。
          审核退回会生成上一个人工步骤的新尝试，旧记录保留。条件分支没走到的步骤记为「未走此分支」，
          它预约的工位时间窗已归还；只有方法标了「可跳过」的步骤才出现跳过按钮。
        </div>
      </Panel>

      <div className="grid cols-2">
        <Panel title={`运行分配（${data.samples.length}）`} flush>
          <div className="wells">
            {data.samples.map((sample) => (
              <div
                key={sample.id}
                className={`well ${sample.state}`}
                title={`${sample.id}｜物理样本 ${sample.physical_sample_id}｜${sample.condition_label}`}
              >
                <b>{sample.well}</b>
                <span className="tiny">{sample.condition_group}</span>
                {sample.is_control ? <span className="tiny tag">对照</span> : null}
              </div>
            ))}
          </div>
          <div className="panel-body small muted">
            运行分配 = 物理样本 × 这一次运行的孔位与条件组。物理实体、流转与检测任务在样本中心，
            结果质量与审核在数据审核页——批次跑完不代表数据可用。
          </div>
        </Panel>

        <Panel title="物料预留与实际投料" flush>
          <table>
            <thead>
              <tr>
                <th>批号</th>
                <th>物料</th>
                <th className="num">授权预留</th>
                <th className="num">已消耗</th>
                <th className="num">剩余占用</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {data.reservations.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.lot_id}</td>
                  <td>
                    {row.material}
                    <div className="tiny muted">{row.release}</div>
                  </td>
                  <td className="num mono">
                    {row.qty} {row.unit}
                  </td>
                  <td className="num mono">{row.consumed_qty}</td>
                  <td className="num mono">
                    <b>{row.outstanding}</b>
                    {row.issued_outstanding !== '0.000000' ? (
                      <div className="tiny warn-text">已领未耗 {row.issued_outstanding}</div>
                    ) : null}
                  </td>
                  <td>
                    <Pill state={row.state === 'consumed' ? 'done' : row.state === 'released' ? 'aborted' : 'scheduled'} label={reservationLabel(row.state)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <TelemetryPanel batchId={batchId} />

      <div className="grid cols-2">
        <Panel title="遥测（最新值）" flush>
          {data.telemetry.length ? (
            <table>
              <thead>
                <tr>
                  <th>工位</th>
                  <th>指标</th>
                  <th className="num">设定</th>
                  <th className="num">实测</th>
                  <th>质量</th>
                  <th>设备时间</th>
                </tr>
              </thead>
              <tbody>
                {data.telemetry.slice(0, 12).map((point, index) => (
                  <tr key={`${point.metric}-${index}`}>
                    <td className="mono">{point.station_id}</td>
                    <td>{point.metric}</td>
                    <td className="num">{num(point.setpoint, 2)}</td>
                    <td className="num">{num(point.value, 2)}</td>
                    <td>
                      <Pill state={point.quality === 'good' ? 'running' : 'fault'} label={point.quality === 'good' ? '可信' : '可疑'} />
                    </td>
                    <td className="small mono">{time(point.device_ts)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>批次开跑后才有遥测数据</Empty>
          )}
        </Panel>

        <Panel title="指令与检查点" flush>
          {data.commands.length ? (
            <table>
              <thead>
                <tr>
                  <th>指令</th>
                  <th>类型</th>
                  <th>工位</th>
                  <th>状态</th>
                  <th>时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.commands.map((command) => (
                  <tr key={command.id}>
                    <td className="mono small">{command.id.slice(0, 8)}</td>
                    <td>
                      第 {command.step_index + 1} 步 {command.type === 'transfer' ? '转运' : command.type}
                      {command.after_command_id && command.state === 'sent' ? (
                        <div className="tiny muted">等前置转运 {command.after_command_id.slice(0, 8)} 完成</div>
                      ) : null}
                      {command.error ? <div className="tiny bad-text">{command.error}</div> : null}
                    </td>
                    <td className="mono">{command.station_id}</td>
                    <td>
                      <Pill state={command.state} label={commandLabel(command.state)} />
                    </td>
                    <td className="small mono">{time(command.created_at)}</td>
                    <td>
                      {['unknown', 'manual'].includes(command.state) ? (
                        <button className="btn sm" onClick={() => setVerifying(command)}>
                          现场核查
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>尚未下发指令</Empty>
          )}
        </Panel>
        <LabwarePanel batchId={batchId} state={data.state} labware={data.labware} />
      </div>

      <div className="grid cols-2">
        <BatchExceptions batchId={batchId} />

        <Panel title="报警" flush>
          {data.alarms.length ? (
            <table>
              <thead>
                <tr>
                  <th>报警</th>
                  <th>内容</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {data.alarms.map((alarm) => (
                  <tr key={alarm.id}>
                    <td className="mono">{alarm.id}</td>
                    <td className="small">{alarm.message}</td>
                    <td>
                      <Pill state={alarm.state} label={alarm.state} />
                      {alarm.condition_active ? <div className="tiny bad-text">条件仍持续</div> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>本批次无报警</Empty>
          )}
        </Panel>

        <Panel title="事件追溯" flush>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>前 → 后</th>
                <th>签名</th>
              </tr>
            </thead>
            <tbody>
              {data.audit.map((event, index) => (
                <tr key={`${event.time}-${index}`}>
                  <td className="small mono">{time(event.time)}</td>
                  <td>
                    {event.action}
                    <div className="tiny muted">
                      {event.user}
                      {event.detail ? ` · ${event.detail}` : ''}
                    </div>
                  </td>
                  <td className="small">
                    {event.before || '—'} → {event.after || '—'}
                  </td>
                  <td>{event.sign ? <Pill state="running" label={`已签名 · ${event.meaning}`} /> : <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      {dialog === 'preflight' ? (
        <PreflightDialog
          batchId={batchId}
          palletCode={data.preflight?.pallet_code ?? ''}
          onClose={() => setDialog(null)}
          onDispatched={() => {
            setDialog(null);
            toast.push(`${batchId} 已签名下发`);
          }}
          sign={sign}
          invalidates={invalidates}
        />
      ) : null}

      {dialog === 'hold' ? (
        <HoldDialog batchId={batchId} onClose={() => setDialog(null)} invalidates={invalidates} />
      ) : null}

      {dialog === 'recover' ? (
        <RecoverDialog
          batchId={batchId}
          onClose={() => setDialog(null)}
          sign={sign}
          invalidates={invalidates}
          onSkip={(stepId) => {
            const step = data.steps.find((row) => row.step_id === stepId);
            if (step) {
              setDialog(null);
              setSkipping(step);
            }
          }}
        />
      ) : null}

      {submitting ? (
        <ManualSubmitDialog
          run={submitting}
          needsMaterialCheck={data.reservations.length > 0}
          onClose={() => setSubmitting(null)}
          invalidates={invalidates}
        />
      ) : null}
      {reviewing ? (
        <StepReviewDialog run={reviewing} onClose={() => setReviewing(null)} invalidates={invalidates} />
      ) : null}
      {viewing ? <RecordDialog run={viewing} onClose={() => setViewing(null)} /> : null}

      {gateDeciding ? (
        <GateDecisionDialog run={gateDeciding} onClose={() => setGateDeciding(null)} invalidates={invalidates} />
      ) : null}
      {branchDeciding ? (
        <BranchDecisionDialog
          run={branchDeciding}
          held={data.state === 'paused'}
          onClose={() => setBranchDeciding(null)}
          invalidates={invalidates}
        />
      ) : null}
      {skipping ? (
        <SkipStepDialog batchId={batchId} step={skipping} onClose={() => setSkipping(null)} invalidates={invalidates} />
      ) : null}
      {signaling !== null ? (
        <SignalDialog batchId={batchId} name={signaling} onClose={() => setSignaling(null)} invalidates={invalidates} />
      ) : null}

      {verifying ? (
        <VerifyCommandDialog
          command={verifying}
          onClose={() => setVerifying(null)}
          sign={sign}
          invalidates={invalidates}
        />
      ) : null}

      {dialog === 'delete' ? (
        <ConfirmDialog
          title={`删除批次 · ${data.id}`}
          danger
          confirmLabel="删除"
          pending={removeBatch.pending}
          error={removeBatch.error?.message}
          onClose={() => setDialog(null)}
          onConfirm={() => removeBatch.run().catch(() => undefined)}
        >
          <div className="note warn">
            该批次尚未下发设备。删除会归还它占的 {data.allocations.length} 个工位时间窗、
            释放 {data.reservations.length} 项物料预留，并移除 {data.samples.length} 个未开跑的样品记录。
          </div>
          <div className="small muted">删除动作写审计；配方快照与实验计划不受影响。</div>
        </ConfirmDialog>
      ) : null}

      {dialog === 'abort' ? (
        <AbortDialog
          batchId={batchId}
          unfinished={data.samples.filter((sample) => sample.state !== 'done').length}
          onClose={() => setDialog(null)}
          sign={sign}
          invalidates={invalidates}
        />
      ) : null}
    </div>
  );
}

type SignFn = (action: string, target: string, meanings: string[]) => Promise<string | null>;

/* 遥测曲线。点位来自执行器写入的设备时间戳序列；配方有黄金批次时把它叠在同一张图上，
   按采样序号对齐——两批的绝对时间不同，能比的是同一步内的走势。 */
function TelemetryPanel({ batchId }: { batchId: string }) {
  const feed = useQuery<TelemetryFeed>(
    `batches:${batchId}:telemetry`,
    () => api.get<TelemetryFeed>(`/batches/${batchId}/telemetry`),
    10000,
  );

  const series = feed.data?.series ?? [];
  if (!series.length) {
    return (
      <Panel title="遥测曲线">
        <Empty>{feed.loading ? '加载中…' : '批次开跑后才有遥测数据'}</Empty>
      </Panel>
    );
  }

  return (
    <Panel
      title={`遥测曲线（${series.length} 条）`}
      aside={
        feed.data?.golden_batch_id ? (
          <span className="small muted">已叠加黄金批次 {feed.data.golden_batch_id}</span>
        ) : (
          <span className="small muted">配方尚未设定黄金批次，无对照曲线</span>
        )
      }
    >
      <div className="grid cols-2">
        {series.map((row) => {
          const points = row.points;
          const suspect = points.filter((point) => point.quality !== 'good').length;
          return (
            <div key={`${row.station_id}-${row.metric}`} className="grid" style={{ gap: 4 }}>
              <div className="small">
                <b>{row.label}</b> <span className="tiny muted mono">{row.station_id} · {row.metric}</span>
                {suspect ? <span className="tag bad">{suspect} 点质量存疑</span> : null}
              </div>
              <LineChart
                series={[
                  { key: 'measured', label: '本批实测', values: points.map((point) => point.v) },
                  ...(row.golden.length
                    ? [{ key: 'golden', label: `黄金批次 ${feed.data?.golden_batch_id}`, values: row.golden, variant: 'golden' as const }]
                    : []),
                ]}
                reference={row.setpoint}
                referenceLabel="设定值"
                xLabels={[time(points[0].t), time(points[points.length - 1].t)]}
                height={150}
              />
            </div>
          );
        })}
      </div>
      <div className="small muted">
        曲线按设备时间戳绘制，不做插值；质量非 good 的点不参与判读。真实工位接入后由适配器流式写入，读侧不变。
      </div>
    </Panel>
  );
}

function PreflightDialog({
  batchId,
  palletCode,
  onClose,
  onDispatched,
  sign,
  invalidates,
}: {
  batchId: string;
  palletCode: string;
  onClose: () => void;
  onDispatched: () => void;
  sign: SignFn;
  invalidates: string[];
}) {
  const [manualReview, setManualReview] = useState(false);
  const [reason, setReason] = useState('');
  const preflight = useQuery<Preflight>(
    `preflight:${batchId}:${manualReview}`,
    () => api.get<Preflight>(`/batches/${batchId}/preflight?manual_review=${manualReview}`),
  );
  const dispatch = useMutation(
    (signatureId: string) =>
      api.post(`/batches/${batchId}/dispatch`, { manual_review: manualReview, reason, signature_id: signatureId }, true),
    { invalidates, onSuccess: onDispatched },
  );

  const checks = preflight.data?.checks ?? [];
  const blockedByOthers = checks.filter((check) => !check.ok && check.key !== 'authority');

  const submit = async () => {
    const signatureId = await sign('下发批次执行', batchId, SIGN_MEANINGS.dispatch);
    if (!signatureId) return;
    await dispatch.run(signatureId).catch(() => undefined);
  };

  return (
    <Modal
      title={`开跑检查 · ${batchId}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!manualReview || blockedByOthers.length > 0 || dispatch.pending}
            title={blockedByOthers.length ? '存在未通过项' : undefined}
            onClick={submit}
          >
            通过检查，签名下发
          </button>
        </>
      }
    >
      <div className="note">
        服务端重新计算七项许可，任一项不过即禁用下发。首工位{' '}
        <span className="mono">{preflight.data?.first_station_id ?? '—'}</span>，
        {preflight.data?.sample_count ?? 0} 个样品。
      </div>
      <CheckList checks={checks} />
      <label className="check">
        <input type="checkbox" checked={manualReview} onChange={(event) => setManualReview(event.target.checked)} />
        已核对托盘条码 <span className="mono">{palletCode}</span> 与物料清单（人工复核）
      </label>
      <Field label="执行理由">
        <textarea
          rows={2}
          value={reason}
          placeholder="例如：物料与托盘复核完成，按批准方法执行"
          onChange={(event) => setReason(event.target.value)}
        />
      </Field>
      {dispatch.error ? <div className="note bad">{dispatch.error.message}</div> : null}
    </Modal>
  );
}

function HoldDialog({ batchId, onClose, invalidates }: { batchId: string; onClose: () => void; invalidates: string[] }) {
  const toast = useToast();
  const [reason, setReason] = useState('');
  const hold = useMutation(
    (text: string) =>
      api.post<{ device_hold_command_id?: string }>(`/batches/${batchId}/hold`, { reason: text }, true),
    {
      invalidates,
      onSuccess: (result) => {
        // 有在途设备动作时，批次只是不再推进新动作；设备是否停住以保持指令回执为准
        toast.push(
          result.device_hold_command_id
            ? `${batchId} 已停止推进，保持指令已发出，等待设备确认`
            : `${batchId} 已进入保持，设备侧没有在途动作`,
        );
        onClose();
      },
    },
  );

  return (
    <Modal
      title={`请求保持 · ${batchId}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={hold.pending} onClick={() => hold.run(reason).catch(() => undefined)}>
            提交保持请求
          </button>
        </>
      }
    >
      <div className="note warn">
        保持按当前步骤能力定义的批准规程执行，不清零累计投料。能力声明不可保持的步骤（如注液封口）
        会被服务端直接拒绝，只能终止或进入人工核查。
      </div>
      <Field label="理由">
        <textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {hold.error ? <div className="note bad">{hold.error.message}</div> : null}
    </Modal>
  );
}

function RecoverDialog({
  batchId,
  onClose,
  sign,
  invalidates,
  onSkip,
}: {
  batchId: string;
  onClose: () => void;
  sign: SignFn;
  invalidates: string[];
  onSkip: (stepId: string) => void;
}) {
  const toast = useToast();
  const evaluation = useQuery<RecoveryEvaluation>(`recovery:${batchId}`, () =>
    api.get<RecoveryEvaluation>(`/batches/${batchId}/recovery-options`),
  );
  const [strategy, setStrategy] = useState('');
  const [verified, setVerified] = useState(false);
  const recover = useMutation(
    (signatureId: string) =>
      api.post(`/batches/${batchId}/recover`, { strategy: strategy || pickDefault(evaluation.data), verified, signature_id: signatureId }, true),
    {
      invalidates,
      onSuccess: () => {
        toast.push(`${batchId} 已执行恢复策略`);
        onClose();
      },
    },
  );

  const data = evaluation.data;
  const selected = strategy || pickDefault(data);

  const submit = async () => {
    const option = data?.options.find((item) => item.id === selected);
    const signatureId = await sign(`恢复策略：${option?.label ?? selected}`, batchId, SIGN_MEANINGS.recover);
    if (!signatureId) return;
    await recover.run(signatureId).catch(() => undefined);
  };

  return (
    <Modal
      title={`恢复评估 · ${batchId}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            维持保持
          </button>
          <button className="btn primary" disabled={!verified || recover.pending} onClick={submit}>
            签名并执行
          </button>
        </>
      }
    >
      {data ? (
        <>
          <div className="note bad">
            保持原因：{data.hold_reason || '—'}（{clock(data.held_at)}）。当前步骤「{data.step_name}」
            保持期间设备状态：{data.hold_state || '无'}。
          </div>
          <CheckList checks={data.preconditions.map((row) => ({ ...row }))} />
          <div className="small muted">
            仅从已核实的检查点继续。不可逆投料步骤不提供重试；恢复前必须核实：
            {data.verify.join('、') || '无'}。
          </div>
          {data.options.map((option) => (
            <div
              key={option.id}
              className={`option${option.allowed ? '' : ' off'}${selected === option.id ? ' sel' : ''}`}
              onClick={() => option.allowed && setStrategy(option.id)}
            >
              <input type="radio" checked={selected === option.id} disabled={!option.allowed} readOnly />
              <div>
                <b>{option.label}</b>
                {option.allowed ? (
                  <div className="impact">{option.impact}</div>
                ) : (
                  <div className="why">不可用：{option.reason}</div>
                )}
              </div>
            </div>
          ))}
          <label className="check">
            <input type="checkbox" checked={verified} onChange={(event) => setVerified(event.target.checked)} />
            已核实上述实际量与设备状态（人工复核）
          </label>
          {recover.error ? <div className="note bad">{recover.error.message}</div> : null}
          <FlowRecovery batchId={batchId} evaluation={data} sign={sign} invalidates={invalidates} onDone={onClose} onSkip={onSkip} />
        </>
      ) : (
        <div className="boot">加载中…</div>
      )}
    </Modal>
  );
}

/* 终止是安全动作：执行门关闭时照样可用，门关着时现场恰恰最需要能停下来。 */
function AbortDialog({
  batchId,
  unfinished,
  onClose,
  sign,
  invalidates,
}: {
  batchId: string;
  unfinished: number;
  onClose: () => void;
  sign: SignFn;
  invalidates: string[];
}) {
  const toast = useToast();
  const [reason, setReason] = useState('');
  const abort = useMutation(
    (signatureId: string) =>
      api.post<{ state: string }>(`/batches/${batchId}/abort`, { reason, signature_id: signatureId }, true),
    {
      invalidates,
      onSuccess: (result) => {
        toast.push(
          result.state === 'aborting'
            ? `${batchId} 已发出终止指令，等待设备确认`
            : `${batchId} 已终止`,
        );
        onClose();
      },
    },
  );

  const submit = async () => {
    const signatureId = await sign('终止批次', batchId, SIGN_MEANINGS.abort);
    if (!signatureId) return;
    await abort.run(signatureId).catch(() => undefined);
  };

  return (
    <Modal
      title={`终止评估 · ${batchId}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            保持现状
          </button>
          <button className="btn danger" disabled={abort.pending} onClick={submit}>
            签名并终止
          </button>
        </>
      }
    >
      <div className="note bad">
        终止将结束本批次，{unfinished} 个未完成样品标记为待隔离处置，预留物料释放回可用量。
        终止不等于设备自动断电或物料自动回收，工位与物料需清理后才释放。
      </div>
      <Field label="理由">
        <textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {abort.error ? <div className="note bad">{abort.error.message}</div> : null}
    </Modal>
  );
}

function pickDefault(evaluation?: RecoveryEvaluation): string {
  return evaluation?.options.find((option) => option.allowed)?.id ?? 'abort';
}

/** 按步骤类型显示它真正的内容：设备看参数，人工看表单字段，等待看方式，审核看角色。 */
function stepContent(step: StepRow): string {
  if (step.kind === 'device') return `${formatParams(step.params)} · ${step.dur} min`;
  if (step.kind === 'manual') {
    const fields = step.form.map((field) => field.label).join('、');
    return `${fields || '无字段'} · ${step.dur} min`;
  }
  if (step.kind === 'wait') {
    return step.wait_for?.mode === 'event'
      ? `等待业务事件 ${step.wait_for.event}`
      : `定时 ${step.dur} min`;
  }
  if (step.kind === 'branch') {
    const mode = { measure: '按测量值', form: '按记录字段', manual: '人工选择' }[step.branch?.mode ?? 'manual'];
    return `${mode} · ${(step.branch?.cases ?? []).map((row) => row.label).join(' / ')}`;
  }
  if (step.kind === 'gate') return '质检关卡';
  if (step.kind === 'split') return '样本拆分';
  return `审核角色 ${step.review_role || 'qa'}`;
}

/** 人工步骤提交。缺必填项、缺样本或物料核对都不推进，服务端会逐项列出缺什么。 */
function ManualSubmitDialog({
  run,
  needsMaterialCheck,
  onClose,
  invalidates,
}: {
  run: StepRunRow;
  needsMaterialCheck: boolean;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const { sign } = useSignature();
  const [values, setValues] = useState<Record<string, unknown>>(run.form_data?.values ?? {});
  const [sampleChecked, setSampleChecked] = useState(false);
  const [materialChecked, setMaterialChecked] = useState(false);
  const [note, setNote] = useState('');

  const submit = useMutation(
    (signatureId: string | null) =>
      api.post(
        `/step-runs/${run.id}/submit`,
        {
          form_data: values,
          checks: { samples: sampleChecked, materials: materialChecked },
          note,
          row_version: run.row_version,
          signature_id: signatureId,
        },
        true,
      ),
    {
      invalidates,
      onSuccess: () => {
        toast.push('人工记录已提交，流程已推进');
        onClose();
      },
    },
  );

  const go = async () => {
    let signatureId: string | null = null;
    if (run.requires_signature) {
      signatureId = await sign('人工步骤记录确认', run.id, ['记录真实完整', '已按 SOP 执行'], run.row_version);
      if (!signatureId) return;
    }
    await submit.run(signatureId).catch(() => undefined);
  };

  return (
    <Modal
      title={`人工记录 · ${run.step_name}（第 ${run.attempt} 次）`}
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={submit.pending} onClick={go}>
            {run.requires_signature ? '签名并提交' : '提交'}
          </button>
        </>
      }
    >
      {run.attempt > 1 ? (
        <div className="note warn">
          这是第 {run.attempt} 次尝试（上一次被审核退回）。旧记录保留，不会被覆盖。
        </div>
      ) : null}
      {run.form.map((field) => (
        <Field key={field.key} label={`${field.label}${field.required === false ? '' : ' *'}`}>
          {field.type === 'number' ? (
            <NumberInput
              value={(values[field.key] as number | '') ?? ''}
              ariaLabel={field.label}
              onChange={(value) => setValues({ ...values, [field.key]: value === '' ? null : value })}
            />
          ) : field.type === 'bool' ? (
            <label className="small">
              <input
                type="checkbox"
                checked={Boolean(values[field.key])}
                onChange={(event) => setValues({ ...values, [field.key]: event.target.checked })}
              />
              确认
            </label>
          ) : field.type === 'enum' ? (
            <select
              value={String(values[field.key] ?? '')}
              onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
            >
              <option value="">选择</option>
              {(field.options ?? []).map((option) => (
                <option key={String(option)} value={String(option)}>
                  {String(option)}
                </option>
              ))}
            </select>
          ) : (
            <input
              value={String(values[field.key] ?? '')}
              onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
            />
          )}
        </Field>
      ))}

      <Field label="核对项" hint="核对是提交的前提，服务端会校验，不是界面上的装饰">
        <label className="small">
          <input type="checkbox" checked={sampleChecked} onChange={(event) => setSampleChecked(event.target.checked)} />
          已核对样本编号与孔位
        </label>
        {needsMaterialCheck ? (
          <label className="small">
            <input
              type="checkbox"
              checked={materialChecked}
              onChange={(event) => setMaterialChecked(event.target.checked)}
            />
            已核对物料批号与用量
          </label>
        ) : null}
      </Field>
      <Field label="备注">
        <textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
      </Field>
      {submit.error ? (
        <div className="note bad">
          {submit.error.message}
          <Blocked reasons={submit.error.blocked.map((row) => row.label)} />
        </div>
      ) : null}
    </Modal>
  );
}

/** 审核节点。批准才继续，退回生成上一个人工步骤的新尝试；不接受任意目标状态。 */
/* 保持中的质检关卡：测量不合格或取不到数值时由 QA 判定。放行要写依据；判不合格按报废处理。 */
function GateDecisionDialog({
  run,
  onClose,
  invalidates,
}: {
  run: StepRunRow;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const { sign } = useSignature();
  const [conclusion, setConclusion] = useState<'approved' | 'rejected'>('rejected');
  const [reason, setReason] = useState('');
  const decide = useMutation(
    (signatureId: string) =>
      api.post(`/step-runs/${run.id}/gate-decision`, { conclusion, reason, signature_id: signatureId }, true),
    {
      invalidates,
      onSuccess: () => {
        toast.push(conclusion === 'approved' ? '已放行，流程继续' : '已判不合格，批次按报废处理');
        onClose();
      },
    },
  );
  const measured = run.form_data as { field?: string; value?: unknown; min?: unknown; max?: unknown } | undefined;
  return (
    <Modal
      title={`质检判定 · ${run.step_name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={decide.pending || !reason.trim()}
            onClick={() =>
              sign(`质检关卡人工判定：${conclusion === 'approved' ? '放行' : '不合格'}`, run.id, ['质检判定属实'])
                .then((signatureId) => (signatureId ? decide.run(signatureId) : undefined))
                .catch((error) => toast.push(error.message))
            }
          >
            签署并提交
          </button>
        </>
      }
    >
      <div className="note warn">{run.reason || '关卡待人工判断'}</div>
      {measured?.field ? (
        <div className="small mono">
          {measured.field} = {String(measured.value ?? '无数值')}（下限 {String(measured.min ?? '—')}，上限{' '}
          {String(measured.max ?? '—')}）
        </div>
      ) : null}
      <Field label="结论">
        <select value={conclusion} onChange={(event) => setConclusion(event.target.value as typeof conclusion)}>
          <option value="rejected">不合格（按报废处理）</option>
          <option value="approved">放行，流程继续</option>
        </select>
      </Field>
      <Field label="判定依据（必填）">
        <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {decide.error ? <div className="note bad">{decide.error.message}</div> : null}
    </Modal>
  );
}

function StepReviewDialog({
  run,
  onClose,
  invalidates,
}: {
  run: StepRunRow;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const { sign } = useSignature();
  const [conclusion, setConclusion] = useState<'approved' | 'rejected'>('approved');
  const [reason, setReason] = useState('');

  const decide = useMutation(
    (signatureId: string) =>
      api.post(
        `/step-runs/${run.id}/review`,
        { conclusion, reason, row_version: run.row_version, signature_id: signatureId },
        true,
      ),
    {
      invalidates,
      onSuccess: () => {
        toast.push(conclusion === 'approved' ? '已批准，流程继续' : '已退回，生成新的人工尝试');
        onClose();
      },
    },
  );

  return (
    <Modal
      title={`审核 · ${run.step_name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={decide.pending || (conclusion === 'rejected' && !reason.trim())}
            onClick={() =>
              sign(
                conclusion === 'approved' ? '流程审核通过' : '流程审核退回',
                run.id,
                conclusion === 'approved' ? ['流程审核通过'] : ['流程审核退回'],
                run.row_version,
              )
                .then((signatureId) => (signatureId ? decide.run(signatureId) : undefined))
                .catch((error) => toast.push(error.message))
            }
          >
            签署并提交
          </button>
        </>
      }
    >
      <div className="note">
        审核本人提交的上游人工记录会被拒（职责分离）。退回不会自动回退重跑已执行的设备步骤——
        上游只有设备步骤时，批次会转入恢复评估。
      </div>
      {run.submitted_by_name ? (
        <div className="small muted">上游记录提交人：{run.submitted_by_name}</div>
      ) : null}
      <Field label="结论">
        <select value={conclusion} onChange={(event) => setConclusion(event.target.value as typeof conclusion)}>
          <option value="approved">批准，继续</option>
          <option value="rejected">退回</option>
        </select>
      </Field>
      <Field label={conclusion === 'rejected' ? '退回理由（必填）' : '意见'}>
        <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {decide.error ? (
        <div className="note bad">
          {decide.error.message}
          <Blocked reasons={decide.error.blocked.map((row) => row.label)} />
        </div>
      ) : null}
    </Modal>
  );
}

function RecordDialog({ run, onClose }: { run: StepRunRow; onClose: () => void }) {
  const values = run.form_data?.values ?? {};
  const checks = run.form_data?.checks ?? {};
  return (
    <Modal title={`记录 · ${run.step_name}（第 ${run.attempt} 次）`} onClose={onClose}>
      <table>
        <tbody>
          {run.form.map((field) => (
            <tr key={field.key}>
              <td className="small muted">{field.label}</td>
              <td className="mono small">{String(values[field.key] ?? '—')}</td>
            </tr>
          ))}
          <tr>
            <td className="small muted">核对</td>
            <td className="small">
              样本 {checks.samples ? '已核对' : '未核对'} · 物料 {checks.materials ? '已核对' : '未核对'}
            </td>
          </tr>
          <tr>
            <td className="small muted">提交人</td>
            <td className="small">{run.submitted_by_name || '—'}</td>
          </tr>
          {run.reviewed_by_name ? (
            <tr>
              <td className="small muted">审核</td>
              <td className="small">
                {run.reviewed_by_name} · {run.conclusion === 'approved' ? '通过' : '退回'}
                {run.reason ? ` · ${run.reason}` : ''}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
      {run.form_data?.note ? <div className="note">{run.form_data.note}</div> : null}
    </Modal>
  );
}

function stepLabel(state: string): string {
  return { pending: '待执行', running: '执行中', held: '保持中', done: '已完成', skipped: '未执行' }[state] ?? state;
}

function commandLabel(state: string): string {
  return (
    {
      sent: '已发送', accepted: '设备已接受', running: '执行中', done: '已完成', unknown: '结果未知',
      manual: '人工核查中', cancelled: '已撤回', not_executed: '确认未执行', partial: '确认部分执行',
    }[state] ?? state
  );
}

/* 结果未知的指令只能由到现场核实的人下结论，并签名负责；系统不替人猜设备动没动。 */
function VerifyCommandDialog({
  command,
  onClose,
  sign,
  invalidates,
}: {
  command: BatchDetail['commands'][number];
  onClose: () => void;
  sign: SignFn;
  invalidates: string[];
}) {
  const toast = useToast();
  const [conclusion, setConclusion] = useState<'executed' | 'not_executed' | 'partial'>('not_executed');
  const [note, setNote] = useState('');
  const verify = useMutation(
    (signatureId: string) =>
      api.post<{ command_state: string; batch_state: string }>(
        `/commands/${command.id}/verify`,
        { conclusion, note, signature_id: signatureId },
        true,
      ),
    {
      invalidates,
      onSuccess: (result) => {
        toast.push(`核查结论已记录；指令 ${commandLabel(result.command_state)}，批次 ${result.batch_state}`);
        onClose();
      },
    },
  );
  const option = VERIFY_OPTIONS.find((row) => row.value === conclusion);
  const submit = async () => {
    const signatureId = await sign(`指令核查：${option?.label ?? conclusion}`, command.id, SIGN_MEANINGS.verify);
    if (!signatureId) return;
    await verify.run(signatureId).catch(() => undefined);
  };

  return (
    <Modal
      title={`现场核查 · 第 ${command.step_index + 1} 步 ${command.type}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={!note.trim() || verify.pending} onClick={submit}>
            签名并记录结论
          </button>
        </>
      }
    >
      <div className="note warn">
        {command.error || '设备侧结果未知'}。请到现场核实工位 <span className="mono">{command.station_id}</span>{' '}
        的实际状态后再下结论。
      </div>
      <Field label="核查结论" hint={option?.hint}>
        <select value={conclusion} onChange={(event) => setConclusion(event.target.value as typeof conclusion)}>
          {VERIFY_OPTIONS.filter((row) => !(command.type === 'hold' && row.value === 'partial')).map((row) => (
            <option key={row.value} value={row.value}>
              {row.label}
            </option>
          ))}
        </select>
      </Field>
      <Field label="核查依据（必填）">
        <textarea
          rows={3}
          value={note}
          placeholder="例如：现场查看设备面板，注液泵计量 0.00 mL，阀门处于关闭位"
          onChange={(event) => setNote(event.target.value)}
        />
      </Field>
      {verify.error ? <div className="note bad">{verify.error.message}</div> : null}
    </Modal>
  );
}

function reservationLabel(state: string): string {
  return { reserved: '预留', consumed: '已消耗', released: '已释放' }[state] ?? state;
}

/* 绑定的载具与它的位置。绑定后每个设备步骤前系统先把板送到工位（转运指令），位置只按回执或扫码更新。 */
function LabwarePanel({ batchId, state, labware }: { batchId: string; state: string; labware: LabwareRow | null }) {
  const { can } = useSession();
  const toast = useToast();
  const [choosing, setChoosing] = useState(false);
  const editable = ['planned', 'scheduled'].includes(state) && can('labware.move');
  const candidates = useQuery<LabwareRow[]>(choosing ? 'labware:bindable' : null, () => api.get<LabwareRow[]>('/labware'));
  const invalidates = [`batches:${batchId}`, 'floor', 'labware'];
  const bind = useMutation((labwareId: string) => api.post(`/batches/${batchId}/labware`, { labware_id: labwareId }), {
    invalidates,
    onSuccess: () => {
      toast.push('载具已绑定');
      setChoosing(false);
    },
  });
  const unbind = useMutation(() => api.remove(`/batches/${batchId}/labware`), { invalidates, onSuccess: () => toast.push('已解绑') });
  const usable = (candidates.data ?? []).filter((row) => !row.batch_active && !['retired', 'lost'].includes(row.state));
  return (
    <Panel
      title="载具与位置"
      aside={
        editable ? (
          <>
            <button className="btn sm" onClick={() => setChoosing((open) => !open)}>
              {labware ? '更换' : '绑定载具'}
            </button>
            {labware ? (
              <button className="btn sm" onClick={() => unbind.run().catch((error) => toast.push(error.message))}>
                解绑
              </button>
            ) : null}
          </>
        ) : null
      }
    >
      {labware ? (
        <div className="small">
          <b className="mono">{labware.barcode}</b> · {labware.type_name}（{labware.rows}×{labware.cols}）
          <div>
            当前位置 <b className="mono">{labware.location_id || '未上线'}</b>{' '}
            {labware.state === 'lost' ? <Pill state="lost" label="位置未知" /> : null}
          </div>
          {labware.in_transit ? (
            <div className="tiny warn-text">
              转运在途：{labware.in_transit.carrier} → {labware.in_transit.to}（{labware.in_transit.state}）
            </div>
          ) : null}
          <div className="tiny muted">
            每个设备步骤开始前，板不在该工位时系统先生成转运指令；设备动作在转运被确认完成后才投递。
          </div>
        </div>
      ) : (
        <div className="small muted">未绑定载具：不做位置追踪，换工位按排程时间窗处理。</div>
      )}
      {choosing ? (
        usable.length ? (
          <select defaultValue="" onChange={(event) => event.target.value && bind.run(event.target.value).catch((error) => toast.push(error.message))}>
            <option value="">选择空闲载具…</option>
            {usable.map((row) => (
              <option key={row.id} value={row.id}>
                {row.barcode} · {row.type_name} · {row.location_id || '未上线'}
              </option>
            ))}
          </select>
        ) : (
          <Empty>没有空闲载具：先在「现场总览」登记</Empty>
        )
      ) : null}
    </Panel>
  );
}


/* ---------- 流程图运行视图 ---------- */

/** 运行时能不能跳过：方法标了可跳过，且这一步还没动（待开始 / 待办 / 等待中）或已明确失败。 */
function canSkip(step: StepRow, batchState: string): boolean {
  if (!step.skippable || !step.run) return false;
  if (!['running', 'paused', 'fault'].includes(batchState)) return false;
  if (['pending', 'ready', 'waiting'].includes(step.run.state)) return step.kind !== 'device' || step.run.state !== 'running';
  return step.run.state === 'failed';
}

function RunGraph({ steps, selected, onSelect }: { steps: StepRow[]; selected: string | null; onSelect: (id: string) => void }) {
  const byId = new Map(steps.map((step) => [step.step_id, step]));
  const nodes: FlowGraphNode[] = steps.map((step) => {
    const outer = step.groups?.[0];
    return {
      id: step.step_id,
      index: step.index,
      state: step.state,
      className: `k-${step.kind}`,
      group: outer ? { id: outer.step_id, label: outer.name } : undefined,
      title: step.run?.reason || undefined,
      content: (
        <>
          <span className="fn-head">
            <span className="mono">{step.index + 1}</span>
            <span className={`kind ${step.kind}`}>{step.kind_label}</span>
            {step.kind === 'device' ? <span className="tag">{step.station_id ?? step.cap_name}</span> : null}
            {step.attempts.length > 1 ? <span className="tag warn">第 {step.attempts.length} 次</span> : null}
          </span>
          <span className="fn-title">{step.name}</span>
          <span className="fn-meta">{step.run?.reason || stepContent(step)}</span>
          <span className="fn-foot">
            <Pill state={step.state} label={step.run?.state_label ?? stepLabel(step.state)} />
            <span className="mono muted">{step.run?.ended_at ? clock(step.run.ended_at) : step.planned_start ? clock(step.planned_start) : ''}</span>
          </span>
        </>
      ),
    };
  });
  const passed = new Set(['completed', 'skipped']);
  const edges: FlowGraphEdge[] = steps.flatMap((step) =>
    (step.after ?? []).map((from) => {
      const parent = byId.get(from);
      const dead = step.state === 'not_taken' || parent?.state === 'not_taken';
      if (parent?.kind === 'branch') {
        const exit = step.when?.[from];
        const label = parent.branch?.cases?.find((row) => row.key === exit)?.label ?? exit;
        const chosen = parent.run?.state === 'completed' ? parent.run.conclusion : '';
        const tone = chosen ? (chosen === exit ? 'taken' : 'dead') : 'branch';
        return { from, to: step.step_id, label, tone } as FlowGraphEdge;
      }
      const tone = dead ? 'dead' : parent && passed.has(parent.state) && step.state !== 'pending' ? 'taken' : undefined;
      return { from, to: step.step_id, tone } as FlowGraphEdge;
    }),
  );
  const loops: FlowGraphLoop[] = steps.flatMap((step) =>
    step.kind === 'branch'
      ? (step.branch?.cases ?? [])
          .filter((row) => row.loop_to && byId.has(row.loop_to))
          .map((row) => ({ from: step.step_id, to: row.loop_to as string, label: row.label }))
      : [],
  );
  return <FlowGraph nodes={nodes} edges={edges} loops={loops} selected={selected} onSelect={onSelect} />;
}

/** 流程图里选中一个步骤后能做的事：和工步表每一行的按钮是同一套判据。 */
function StepActions({
  step,
  batchState,
  onSubmit,
  onReview,
  onGate,
  onBranch,
  onSkip,
  onSignal,
}: {
  step: StepRow | null;
  batchState: string;
  onSubmit: (run: StepRunRow) => void;
  onReview: (run: StepRunRow) => void;
  onGate: (run: StepRunRow) => void;
  onBranch: (run: StepRunRow) => void;
  onSkip: (step: StepRow) => void;
  onSignal: (name: string) => void;
}) {
  const { can } = useSession();
  if (!step) return null;
  const run = step.run;
  const open = run && ['ready', 'running'].includes(run.state);
  const actions: JSX.Element[] = [];
  if (run && open && step.kind === 'manual' && can('step.submit')) {
    actions.push(<button key="submit" className="btn sm primary" onClick={() => onSubmit(run)}>填写记录</button>);
  }
  if (run && open && step.kind === 'review' && can('step.review')) {
    actions.push(<button key="review" className="btn sm primary" onClick={() => onReview(run)}>审核</button>);
  }
  if (run && run.state === 'ready' && step.kind === 'gate' && can('step.review')) {
    actions.push(<button key="gate" className="btn sm primary" onClick={() => onGate(run)}>质检判定</button>);
  }
  if (run && run.state === 'ready' && step.kind === 'branch' && (can('step.submit') || can('step.review'))) {
    actions.push(<button key="branch" className="btn sm primary" onClick={() => onBranch(run)}>选择出口</button>);
  }
  if (run && run.state === 'waiting' && step.wait_for?.mode === 'event' && can('batch.signal')) {
    actions.push(<button key="signal" className="btn sm" onClick={() => onSignal(step.wait_for?.event ?? '')}>发出事件 {step.wait_for?.event}</button>);
  }
  if (canSkip(step, batchState) && can('batch.recover')) {
    actions.push(<button key="skip" className="btn sm" onClick={() => onSkip(step)}>跳过</button>);
  }
  return (
    <div className="panel-body row">
      <b>
        {step.index + 1}. {step.name}
      </b>
      <Pill state={step.state} label={run?.state_label ?? stepLabel(step.state)} />
      {run?.deadline_at ? (
        <span className={`tiny ${run.timed_out_at ? 'bad-text' : 'muted'}`}>
          截止 {time(run.deadline_at)}{run.timed_out_at ? '（已超时）' : ''}
        </span>
      ) : null}
      {run?.reason ? <span className="small muted">{run.reason}</span> : null}
      <span className="row">{actions.length ? actions : <span className="tiny muted">当前没有可执行的操作</span>}</span>
    </div>
  );
}

/* 条件分支选出口。人工选择模式是普通待办；判据缺失或回环到上限而保持的分支要 QA 签名。 */
function BranchDecisionDialog({
  run,
  held,
  onClose,
  invalidates,
}: {
  run: StepRunRow;
  held: boolean;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const { sign } = useSignature();
  const forward = run.branch_cases.filter((row) => !row.loop);
  const [choice, setChoice] = useState(forward[0]?.key ?? run.branch_cases[0]?.key ?? '');
  const [reason, setReason] = useState('');
  const needsSignature = held || run.requires_signature;
  const decide = useMutation(
    (signatureId: string | null) =>
      api.post(
        `/step-runs/${run.id}/branch-decision`,
        { case: choice, reason, row_version: run.row_version, signature_id: signatureId },
        true,
      ),
    {
      invalidates,
      onSuccess: () => {
        toast.push('已选择出口，流程继续');
        onClose();
      },
    },
  );
  const evidence = run.form_data as { field?: string; value?: unknown };
  const submit = async () => {
    if (!needsSignature) {
      await decide.run(null).catch(() => undefined);
      return;
    }
    const label = run.branch_cases.find((row) => row.key === choice)?.label ?? choice;
    const signatureId = await sign(`条件分支选择出口：${label}`, run.id, ['分支判定属实']);
    if (signatureId) await decide.run(signatureId).catch(() => undefined);
  };
  return (
    <Modal
      title={`选择出口 · ${run.step_name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={decide.pending || !choice || !reason.trim()} onClick={submit}>
            {needsSignature ? '签署并提交' : '提交'}
          </button>
        </>
      }
    >
      {run.reason ? <div className={`note${held ? ' warn' : ''}`}>{run.reason}</div> : null}
      {evidence?.field ? (
        <div className="small mono">
          判据 {evidence.field} = {String(evidence.value ?? '无数值')}
        </div>
      ) : null}
      <Field label="出口">
        <select value={choice} onChange={(event) => setChoice(event.target.value)}>
          {run.branch_cases.map((row) => (
            <option key={row.key} value={row.key}>
              {row.label}
              {row.loop ? '（回环：作废回环体后重做）' : ''}
            </option>
          ))}
        </select>
      </Field>
      <Field label="依据（必填）">
        <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {decide.error ? <div className="note bad">{decide.error.message}</div> : null}
    </Modal>
  );
}

/* 跳过步骤：只有方法标了可跳过的才行，理由必填并签名。设备已收到指令、结果未知时服务端会拒绝。 */
function SkipStepDialog({
  batchId,
  step,
  onClose,
  invalidates,
}: {
  batchId: string;
  step: StepRow;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const { sign } = useSignature();
  const [reason, setReason] = useState('');
  const skip = useMutation(
    (signatureId: string) =>
      api.post(`/batches/${batchId}/skip`, { step_id: step.step_id, reason, signature_id: signatureId }, true),
    {
      invalidates,
      onSuccess: () => {
        toast.push(`第 ${step.index + 1} 步已跳过，流程继续`);
        onClose();
      },
    },
  );
  return (
    <Modal
      title={`跳过步骤 · ${step.index + 1}. ${step.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={skip.pending || !reason.trim()}
            onClick={() =>
              sign(`跳过步骤：${step.name}`, batchId, ['确认该步骤可以跳过', '接受偏差风险'])
                .then((signatureId) => (signatureId ? skip.run(signatureId) : undefined))
                .catch((error) => toast.push(error.message))
            }
          >
            签署并跳过
          </button>
        </>
      }
    >
      <div className="note warn">
        跳过后这一步不会执行，后继照常开出；原记录保留，审计写明跳过理由与签名。设备已经收到这一步的指令、
        或指令结果未知时不能跳过。
      </div>
      <Field label="跳过理由（必填）">
        <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
      </Field>
      {skip.error ? (
        <div className="note bad">
          {skip.error.message}
          <Blocked reasons={skip.error.blocked.map((row) => row.label)} />
        </div>
      ) : null}
    </Modal>
  );
}

/* 发出批次业务事件：唤醒等着它的事件等待节点。外部系统走 /runtime/batches/{id}/signals（服务身份）。 */
function SignalDialog({
  batchId,
  name: initial,
  onClose,
  invalidates,
}: {
  batchId: string;
  name: string;
  onClose: () => void;
  invalidates: string[];
}) {
  const toast = useToast();
  const [name, setName] = useState(initial);
  const [note, setNote] = useState('');
  // 同一次对话框里的重复点击是同一条事件：服务端按 event_id 去重，不会唤醒两个等待节点
  const [eventId] = useState(() => idempotencyKey());
  const send = useMutation(
    () => api.post(`/batches/${batchId}/signals`, { name, event_id: eventId, payload: note ? { note } : {} }),
    {
      invalidates,
      onSuccess: () => {
        toast.push(`已发出事件 ${name}`);
        onClose();
      },
    },
  );
  return (
    <Modal
      title="发出业务事件"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={send.pending || !name.trim()} onClick={() => send.run().catch(() => undefined)}>
            发出
          </button>
        </>
      }
    >
      <div className="small muted">
        事件会唤醒本批次里等着同名事件的节点；还没开出的等待节点开出时会直接消费这条事件。
      </div>
      <Field label="事件名">
        <input value={name} onChange={(event) => setName(event.target.value)} />
      </Field>
      <Field label="说明（写入事件载荷）">
        <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="如：样品已由物流送达 3 号柜" />
      </Field>
      {send.error ? <div className="note bad">{send.error.message}</div> : null}
    </Modal>
  );
}

/* 恢复评估里的流程级选项：跳过当前步骤、从指定节点重做。 */
function FlowRecovery({
  batchId,
  evaluation,
  sign,
  invalidates,
  onDone,
  onSkip,
}: {
  batchId: string;
  evaluation: RecoveryEvaluation;
  sign: SignFn;
  invalidates: string[];
  onDone: () => void;
  onSkip: (stepId: string) => void;
}) {
  const toast = useToast();
  const [target, setTarget] = useState('');
  const [reason, setReason] = useState('');
  const rerun = useMutation(
    (signatureId: string) =>
      api.post(`/batches/${batchId}/rerun`, { step_id: target, reason, signature_id: signatureId }, true),
    {
      invalidates,
      onSuccess: () => {
        toast.push('已从指定节点重做');
        onDone();
      },
    },
  );
  const skip = evaluation.skip;
  const targets = evaluation.rerun_targets ?? [];
  if (!skip && !targets.length) return null;
  return (
    <div className="deps">
      <b className="small">流程级处理</b>
      {skip ? (
        <div className="row">
          <button className="btn sm" disabled={!skip.allowed} title={skip.reason || undefined} onClick={() => onSkip(skip.step_id)}>
            跳过当前步骤
          </button>
          {!skip.allowed ? <span className="tiny muted">不可用：{skip.reason}</span> : null}
        </div>
      ) : null}
      {targets.length ? (
        <>
          <div className="small muted">
            从指定节点重做：该步与它的全部下游记录作废（保留），从该步重新开出；会重新执行的设备步骤写进审计。
          </div>
          <div className="row">
            <select value={target} onChange={(event) => setTarget(event.target.value)}>
              <option value="">选择重做起点</option>
              {targets.map((row) => (
                <option key={row.step_id} value={row.step_id}>
                  第 {row.index + 1} 步 · {row.name}
                </option>
              ))}
            </select>
            <input value={reason} placeholder="重做理由（必填）" onChange={(event) => setReason(event.target.value)} />
            <button
              className="btn sm danger"
              disabled={!target || !reason.trim() || rerun.pending}
              onClick={() =>
                sign('从指定节点重做', batchId, ['已核实现场状态', '接受重新执行设备步骤'])
                  .then((signatureId) => (signatureId ? rerun.run(signatureId) : undefined))
                  .catch((error) => toast.push(error.message))
              }
            >
              签名并重做
            </button>
          </div>
          {rerun.error ? <div className="note bad">{rerun.error.message}</div> : null}
        </>
      ) : (
        <div className="tiny muted">存在没有结论的设备指令，先完成现场核查才能从指定节点重做。</div>
      )}
    </div>
  );
}


/* 本批次的异常事件：自动处理做了什么、结果如何、最终怎么收尾。处理入口在异常中心。 */
function BatchExceptions({ batchId }: { batchId: string }) {
  const events = useQuery<ExceptionEventRow[]>(`exceptions:batch:${batchId}`, () =>
    api.get<ExceptionEventRow[]>(`/exceptions?batch_id=${batchId}`), 15000,
  );
  if (!events.data?.length) return null;
  return (
    <Panel title={`异常事件（${events.data.length}）`} aside={<Link to="/exceptions" className="small">异常中心</Link>} flush>
      <table>
        <tbody>
          {events.data.map((row) => (
            <tr key={row.id}>
              <td className="small mono">{time(row.created_at)}</td>
              <td>
                <span className="tag">{row.category_label}</span>
                {row.step_index >= 0 ? <span className="tiny muted"> 第 {row.step_index + 1} 步</span> : null}
                <div className="tiny muted">{row.message}</div>
              </td>
              <td className="small">
                {row.auto_action ? `${row.auto_action_label}：${row.auto_result}` : row.decision}
                {row.final_result ? <div className="tiny muted">最终：{row.final_result}</div> : null}
              </td>
              <td>
                <Pill state={{ open: 'fault', manual: 'paused', auto_resolved: 'running' }[row.state] ?? 'done'} label={row.state_label} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}
