import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api, pageQuery } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { QrLabel } from '../../shared/labels';
import type { LocationRow, Paged, SampleDetail, SampleLocation, SampleRow } from '../../shared/types';
import {
  ConfirmDialog, Empty, Field, ListState, Modal, NumberInput, Pager, Panel, Pill, useToast,
} from '../../shared/ui';

const LIFECYCLE: [string, string][] = [
  ['registered', '已登记'],
  ['received', '已收样'],
  ['in_use', '使用中'],
  ['stored', '已入库'],
  ['exhausted', '已用尽'],
  ['disposed', '已处置'],
];

/** 扫码事件键。重复扫描靠它去重，所以同一次扫描必须用同一个键。 */
function scanKey(sampleId: string, kind: string): string {
  return `${kind}:${sampleId}:${Date.now()}`;
}

export function SamplesPage() {
  const { can } = useSession();
  const toast = useToast();
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [state, setState] = useState('');
  const [registering, setRegistering] = useState(false);
  const [scan, setScan] = useState('');
  const [lookup, setLookup] = useState('');

  const query = pageQuery({ page, page_size: 20, keyword, state });
  const samples = useQuery<Paged<SampleRow>>(
    `samples:${query}`, () => api.get<Paged<SampleRow>>(`/samples${query}`),
  );

  const receive = useMutation(
    (barcode: string) =>
      api.post(`/samples/${encodeURIComponent(barcode)}/receive`, {
        event_key: scanKey(barcode, 'receive'),
        confirm_method: 'barcode',
      }),
    {
      invalidates: ['samples', 'dashboard'],
      onSuccess: (result) => {
        const row = result as { id: string; replayed: boolean; hint?: string };
        toast.push(row.replayed ? row.hint ?? '该扫码事件已记录，未重复写入' : `${row.id} 已收样`);
        setScan('');
      },
    },
  );

  const rows = samples.data?.items ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>样本管理</h1>
        <span className="small muted">
          物理样本与「这一次运行里的位置」是两件事。登记样本不要求先有批次，也不要求已有检测结果。
        </span>
      </div>

      <Panel title="扫码接收">
        <div className="note">
          条码用键盘输入模式：扫码枪输入后回车提交。重复扫描同一个样本不会多写一条交接记录。
        </div>
        <form
          className="filters"
          onSubmit={(event) => {
            event.preventDefault();
            if (scan.trim()) receive.run(scan.trim()).catch((error) => toast.push(error.message));
          }}
        >
          <input
            autoFocus
            placeholder="扫描或输入样本条码后回车"
            value={scan}
            onChange={(event) => setScan(event.target.value)}
          />
          <button className="btn primary sm" type="submit" disabled={!scan.trim() || receive.pending}>
            收样
          </button>
        </form>
        {receive.error ? <div className="note bad">{receive.error.message}</div> : null}
        <form
          className="filters"
          onSubmit={(event) => {
            event.preventDefault();
            if (lookup.trim()) navigate(`/samples/${encodeURIComponent(lookup.trim())}`);
          }}
        >
          <input placeholder="扫码查找：样本条码或编号后回车" value={lookup} onChange={(event) => setLookup(event.target.value)} />
          <button className="btn sm" type="submit" disabled={!lookup.trim()}>
            打开
          </button>
        </form>
      </Panel>

      <Panel
        title={`物理样本（${samples.data?.total ?? 0}）`}
        aside={
          <div className="filters">
            <input
              placeholder="编号、条码或来源"
              value={keyword}
              onChange={(event) => {
                setKeyword(event.target.value);
                setPage(1);
              }}
            />
            <select
              value={state}
              onChange={(event) => {
                setState(event.target.value);
                setPage(1);
              }}
            >
              <option value="">全部生命周期</option>
              {LIFECYCLE.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            {can('sample.register') ? (
              <button className="btn primary sm" onClick={() => setRegistering(true)}>
                登记样本
              </button>
            ) : null}
          </div>
        }
        flush
      >
        <ListState
          loading={samples.loading && !samples.data}
          error={samples.error}
          empty={!rows.length}
          emptyText="没有符合条件的样本"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>样本</th>
                <th>来源</th>
                <th>数量</th>
                <th>当前位置</th>
                <th>生命周期</th>
                <th>关联</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="clickable" onClick={() => navigate(`/samples/${row.id}`)}>
                  <td>
                    <b className="mono">{row.id}</b>
                    <div className="tiny muted">条码 {row.barcode || '—'}</div>
                  </td>
                  <td className="small">
                    {row.source || '—'}
                    {row.parent_id ? <div className="tiny muted">分自 {row.parent_id}</div> : null}
                  </td>
                  <td className="mono small">
                    {row.quantity ? `${row.quantity} ${row.unit}` : '未录'}
                  </td>
                  <td className="small">
                    {row.location?.text || row.current_location || row.location_note || '—'}
                    <div className="tiny muted">保管 {row.custodian || '—'}</div>
                  </td>
                  <td>
                    <Pill state={row.lifecycle_state} label={row.lifecycle_label} />
                  </td>
                  <td className="small muted">
                    {row.assignment_count} 次运行 · {row.child_count} 个子样
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <Pager
          page={samples.data?.page ?? 1}
          pageSize={samples.data?.page_size ?? 20}
          total={samples.data?.total ?? 0}
          onChange={setPage}
        />
      </Panel>

      {registering ? <RegisterDialog onClose={() => setRegistering(false)} /> : null}
    </div>
  );
}

export function SampleDetailPage() {
  const { sampleId = '' } = useParams();
  const { can } = useSession();
  const toast = useToast();
  const navigate = useNavigate();
  const detail = useQuery<SampleDetail>(
    `samples:${sampleId}`, () => api.get<SampleDetail>(`/samples/${sampleId}`),
  );
  const [splitting, setSplitting] = useState(false);
  const [transferring, setTransferring] = useState(false);
  const [disposing, setDisposing] = useState(false);

  const dispose = useMutation(
    (reason: string) => api.post(`/samples/${sampleId}/dispose`, { reason }),
    {
      invalidates: ['samples'],
      onSuccess: () => {
        toast.push('样本已处置');
        setDisposing(false);
      },
    },
  );

  const sample = detail.data;
  if (!sample) {
    return (
      <div className="page">
        <ListState loading={detail.loading} error={detail.error} />
      </div>
    );
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>
            {sample.id} <Pill state={sample.lifecycle_state} label={sample.lifecycle_label} />
          </h1>
          <span className="small muted">
            条码 {sample.barcode || '—'} · 来源 {sample.source || '—'} ·
            {sample.quantity ? ` 剩余 ${sample.quantity} ${sample.unit}` : ' 数量未录'}
          </span>
        </div>
        <div className="panel-aside">
          {can('sample.transfer') ? (
            <button className="btn sm" onClick={() => setTransferring(true)}>
              交接 / 移动
            </button>
          ) : null}
          {can('sample.register') ? (
            <button className="btn sm" onClick={() => setSplitting(true)}>
              分样
            </button>
          ) : null}
          {can('sample.dispose') && sample.lifecycle_state !== 'disposed' ? (
            <button className="btn sm danger" onClick={() => setDisposing(true)}>
              处置
            </button>
          ) : null}
        </div>
      </div>

      {sample.location_note ? <div className="banner warn">{sample.location_note}</div> : null}

      <div className="grid cols-2">
        <Panel title="当前位置">
          <LocationView location={sample.location} fallback={sample.current_location} />
        </Panel>
        <Panel title="标签">
          <QrLabel path={`/samples/${sample.id}/qr`} cacheKey={`samples:${sample.id}:qr`} />
        </Panel>
      </div>

      <div className="split">
        <div className="stack">
          <Panel title="来源谱系与分样" flush>
            {sample.lineage.length || sample.children.length ? (
              <table>
                <thead>
                  <tr>
                    <th>关系</th>
                    <th>样本</th>
                    <th>数量</th>
                    <th>生命周期</th>
                  </tr>
                </thead>
                <tbody>
                  {sample.lineage.map((row) => (
                    <tr key={row.id} className="clickable" onClick={() => navigate(`/samples/${row.id}`)}>
                      <td className="small muted">上游</td>
                      <td className="mono">{row.id}</td>
                      <td className="mono small">{row.quantity ? `${row.quantity} ${row.unit}` : '—'}</td>
                      <td>
                        <Pill state={row.lifecycle_state} label={row.lifecycle_label} />
                      </td>
                    </tr>
                  ))}
                  {sample.children.map((row) => (
                    <tr key={row.id} className="clickable" onClick={() => navigate(`/samples/${row.id}`)}>
                      <td className="small muted">子样</td>
                      <td className="mono">{row.id}</td>
                      <td className="mono small">{row.quantity ? `${row.quantity} ${row.unit}` : '—'}</td>
                      <td>
                        <Pill state={row.lifecycle_state} label={row.lifecycle_label} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>没有上游或子样</Empty>
            )}
          </Panel>

          <Panel title="运行分配" flush>
            {sample.assignments.length ? (
              <table>
                <thead>
                  <tr>
                    <th>批次</th>
                    <th>容器 / 孔位</th>
                    <th>条件组</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {sample.assignments.map((row) => (
                    <tr key={row.id} className="clickable" onClick={() => navigate(`/batches/${row.batch_id}`)}>
                      <td className="mono">{row.batch_id}</td>
                      <td className="mono small">
                        {row.container_id || '—'} / {row.well}
                      </td>
                      <td className="small">
                        {row.condition_group} {row.condition_label}
                        {row.repeat ? <span className="tiny muted"> · 第 {row.repeat} 次重复</span> : null}
                      </td>
                      <td>
                        <Pill state={row.state} />
                        {row.legacy_quality ? (
                          <div className="tiny muted">历史质量标记 {row.legacy_quality}</div>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>该样本还没有参与任何运行</Empty>
            )}
          </Panel>

          <Panel title="检测任务" flush>
            {sample.analysis_tasks.length ? (
              <table>
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>轮次</th>
                    <th>检测方法版本</th>
                    <th>采集状态</th>
                  </tr>
                </thead>
                <tbody>
                  {sample.analysis_tasks.map((row) => (
                    <tr key={row.id}>
                      <td className="mono small">
                        {row.id.slice(0, 8)}
                        {row.retest_of ? <div className="tiny muted">重测自 {row.retest_of.slice(0, 8)}</div> : null}
                      </td>
                      <td className="small">第 {row.round_no} 轮</td>
                      <td className="small">{row.method_version || row.method || '—'}</td>
                      <td>
                        <Pill state={row.state} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>还没有检测任务</Empty>
            )}
          </Panel>
        </div>

        <div className="stack">
          <Panel title="流转时间线">
            {sample.transfers.length ? (
              <ul className="timeline">
                {sample.transfers.map((row) => (
                  <li key={row.id}>
                    <time>{clock(row.occurred_at)}</time>
                    <div>
                      <b>{row.kind_label}</b>
                      <div className="small">
                        {row.from_location || '—'} → {row.to_location || '—'}
                      </div>
                      <div className="tiny muted">
                        {row.from_party || '—'} → {row.to_party || '—'} · 确认 {row.confirm_method}
                        {row.quantity ? ` · ${row.quantity} ${row.unit}` : ''}
                      </div>
                      {row.note ? <div className="tiny muted">{row.note}</div> : null}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>没有流转记录</Empty>
            )}
          </Panel>

          <Panel title="孔位占用">
            {sample.slots.length ? (
              <ul className="timeline">
                {sample.slots.map((row, index) => (
                  <li key={index}>
                    <time>{clock(row.occupied_at)}</time>
                    <div>
                      <span className="mono">
                        {row.container_id} / {row.well}
                      </span>
                      <div className="tiny muted">
                        {row.released_at ? `已释放 ${clock(row.released_at)}` : '在途占用中'}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>没有孔位占用记录</Empty>
            )}
          </Panel>
        </div>
      </div>

      {splitting ? <SplitDialog sample={sample} onClose={() => setSplitting(false)} /> : null}
      {transferring ? <TransferDialog sample={sample} onClose={() => setTransferring(false)} /> : null}
      {disposing ? (
        <ConfirmDialog
          title={`处置 · ${sample.id}`}
          danger
          confirmLabel="处置"
          reasonLabel="处置理由"
          pending={dispose.pending}
          error={dispose.error?.message}
          onConfirm={(reason) => dispose.run(reason).catch(() => undefined)}
          onClose={() => setDisposing(false)}
        >
          <div className="note warn">处置后样本不可再参与运行；历史记录与谱系保留。</div>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function RegisterDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({
    id: '', barcode: '', source: '', sample_type: '', unit: 'g', storage_condition: '',
    current_location: '',
  });
  const [quantity, setQuantity] = useState<number | ''>('');

  const create = useMutation(
    () =>
      api.post(
        '/samples',
        { ...form, quantity: quantity === '' ? null : String(quantity) },
        true,
      ),
    {
      invalidates: ['samples', 'dashboard'],
      onSuccess: (result) => {
        toast.push(`${(result as { id: string }).id} 已登记`);
        onClose();
      },
    },
  );

  return (
    <Modal
      title="登记样本"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" disabled={create.pending} onClick={() => create.run().catch(() => undefined)}>
            登记
          </button>
        </>
      }
    >
      <div className="note">
        不需要先有批次，也不需要已有检测结果。编号留空由系统按 PS-年月-序号 生成。
      </div>
      <Field label="样本编号" hint="留空自动生成">
        <input value={form.id} onChange={(event) => setForm({ ...form, id: event.target.value })} />
      </Field>
      <Field label="条码" hint="留空则与编号相同；组织内唯一">
        <input value={form.barcode} onChange={(event) => setForm({ ...form, barcode: event.target.value })} />
      </Field>
      <Field label="来源">
        <input value={form.source} onChange={(event) => setForm({ ...form, source: event.target.value })} />
      </Field>
      <Field label="样本类型">
        <input value={form.sample_type} onChange={(event) => setForm({ ...form, sample_type: event.target.value })} />
      </Field>
      <Field label="数量" hint="分样要核对母样剩余量，所以数量最好现在就录">
        <div className="filters">
          <NumberInput value={quantity} onChange={setQuantity} />
          <input
            style={{ maxWidth: 80 }}
            value={form.unit}
            onChange={(event) => setForm({ ...form, unit: event.target.value })}
          />
        </div>
      </Field>
      <Field label="保存条件">
        <input
          value={form.storage_condition}
          onChange={(event) => setForm({ ...form, storage_condition: event.target.value })}
        />
      </Field>
      <Field label="当前位置">
        <input
          value={form.current_location}
          onChange={(event) => setForm({ ...form, current_location: event.target.value })}
        />
      </Field>
      {create.error ? <div className="note bad">{create.error.message}</div> : null}
    </Modal>
  );
}

function SplitDialog({ sample, onClose }: { sample: SampleDetail; onClose: () => void }) {
  const toast = useToast();
  const [children, setChildren] = useState<{ quantity: number | ''; id: string }[]>([
    { quantity: '', id: '' },
  ]);
  const [loss, setLoss] = useState<number | ''>('');
  const [lossReason, setLossReason] = useState('');
  const [eventKey] = useState(() => scanKey(sample.id, 'split'));

  const split = useMutation(
    () =>
      api.post(
        `/samples/${sample.id}/split`,
        {
          event_key: eventKey,
          children: children
            .filter((row) => row.quantity !== '')
            .map((row) => ({ id: row.id, quantity: String(row.quantity) })),
          loss: loss === '' ? '0' : String(loss),
          loss_reason: lossReason,
        },
        true,
      ),
    {
      invalidates: ['samples'],
      onSuccess: () => {
        toast.push('分样完成');
        onClose();
      },
    },
  );

  const total = children.reduce((sum, row) => sum + (row.quantity === '' ? 0 : Number(row.quantity)), 0);
  const withLoss = total + (loss === '' ? 0 : Number(loss));
  const remaining = Number(sample.quantity ?? 0);

  return (
    <Modal
      title={`分样 · ${sample.id}`}
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={split.pending || !total || withLoss > remaining}
            onClick={() => split.run().catch(() => undefined)}
          >
            分样
          </button>
        </>
      }
    >
      {sample.quantity === null ? (
        <div className="note bad">母样没有录入数量，无法核对是否超量。请先补录数量。</div>
      ) : (
        <div className="note">
          母样剩余 <b className="mono">{sample.quantity}</b> {sample.unit}；
          子样合计 <b className="mono">{total.toFixed(6)}</b> + 损耗{' '}
          <b className="mono">{(loss === '' ? 0 : Number(loss)).toFixed(6)}</b> ={' '}
          <b className="mono">{withLoss.toFixed(6)}</b>
          {withLoss > remaining ? <span className="bad-text"> · 已超出母样剩余量</span> : null}
        </div>
      )}

      {children.map((row, index) => (
        <div className="filters" key={index} style={{ marginBottom: 8 }}>
          <input
            placeholder={`子样编号（留空自动生成 ${sample.id}-${String(index + 1).padStart(2, '0')}）`}
            value={row.id}
            onChange={(event) => {
              const next = [...children];
              next[index] = { ...row, id: event.target.value };
              setChildren(next);
            }}
          />
          <NumberInput
            value={row.quantity}
            ariaLabel={`子样 ${index + 1} 数量`}
            onChange={(value) => {
              const next = [...children];
              next[index] = { ...row, quantity: value };
              setChildren(next);
            }}
          />
          <span className="small muted">{sample.unit}</span>
          {children.length > 1 ? (
            <button
              className="btn sm"
              onClick={() => setChildren(children.filter((_, position) => position !== index))}
            >
              移除
            </button>
          ) : null}
        </div>
      ))}
      <button className="btn sm" onClick={() => setChildren([...children, { quantity: '', id: '' }])}>
        增加子样
      </button>

      <Field label="损耗" hint="记录损耗必须写明原因；不允许用损耗掩盖超量分样">
        <div className="filters">
          <NumberInput value={loss} onChange={setLoss} />
          <input
            placeholder="损耗原因"
            value={lossReason}
            onChange={(event) => setLossReason(event.target.value)}
          />
        </div>
      </Field>
      {split.error ? <div className="note bad">{split.error.message}</div> : null}
    </Modal>
  );
}

function LocationView({ location, fallback }: { location?: SampleLocation; fallback: string }) {
  if (!location || location.kind === 'none') return <div className="small muted">{fallback || '位置未登记'}</div>;
  if (location.kind === 'labware' && location.labware) {
    return (
      <div className="small">
        <div>
          载具 <b className="mono">{location.labware.barcode}</b>（{location.labware.type_name}）孔位 <b className="mono">{location.well || '—'}</b>
        </div>
        <div className="muted">
          载具在 {location.place ? `${location.place.name}${location.place.station_id ? ` · ${location.place.station_id}` : ''}` : '未上线（未扫码放置）'}
          {location.labware.state === 'lost' ? ' · 载具位置未知，需扫码重新定位' : ''}
        </div>
      </div>
    );
  }
  if (location.kind === 'location' && location.place) {
    return (
      <div className="small">
        登记位置 <b>{location.place.name}</b> <span className="tiny muted mono">{location.place.id}</span>
      </div>
    );
  }
  return <div className="small">{location.text}<div className="tiny muted">自由文本位置（未对应登记位置）</div></div>;
}

function TransferDialog({ sample, onClose }: { sample: SampleDetail; onClose: () => void }) {
  const toast = useToast();
  const locations = useQuery<LocationRow[]>('locations', () => api.get<LocationRow[]>('/locations'));
  const [form, setForm] = useState({
    kind: 'handover',
    to_location: '',
    to_location_id: '',
    to_party: '',
    note: '',
    confirm_method: 'barcode',
  });
  const [eventKey] = useState(() => scanKey(sample.id, 'transfer'));

  const transfer = useMutation(
    () => api.post(`/samples/${sample.id}/transfers`, { ...form, event_key: eventKey }),
    {
      invalidates: ['samples'],
      onSuccess: (result) => {
        const row = result as { replayed: boolean; hint?: string };
        toast.push(row.replayed ? row.hint ?? '该交接事件已记录' : '交接已记录');
        onClose();
      },
    },
  );

  return (
    <Modal
      title={`交接 · ${sample.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={transfer.pending}
            onClick={() => transfer.run().catch(() => undefined)}
          >
            记录
          </button>
        </>
      }
    >
      <div className="note">
        交接记录源位置、目标位置、双方与时间。同一个事件键重复提交不会多写一条。
      </div>
      <Field label="类型">
        <select value={form.kind} onChange={(event) => setForm({ ...form, kind: event.target.value })}>
          <option value="handover">交接</option>
          <option value="move">移动</option>
          <option value="store">入库存放</option>
        </select>
      </Field>
      <Field label="源位置">
        <input readOnly value={sample.current_location || '—'} />
      </Field>
      <Field label="目标位置" hint="选登记过的库位 / 放置位，样本的结构化位置随之更新；也可以写自由文本（外部交接）">
        <div className="filters">
          <select value={form.to_location_id} onChange={(event) => setForm({ ...form, to_location_id: event.target.value })}>
            <option value="">不选登记位置</option>
            {(locations.data ?? []).filter((row) => row.active).map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}（{row.id}）
              </option>
            ))}
          </select>
          {form.to_location_id ? null : (
            <input placeholder="自由文本位置" value={form.to_location} onChange={(event) => setForm({ ...form, to_location: event.target.value })} />
          )}
        </div>
      </Field>
      <Field label="接收人">
        <input value={form.to_party} onChange={(event) => setForm({ ...form, to_party: event.target.value })} />
      </Field>
      <Field label="确认方式">
        <select
          value={form.confirm_method}
          onChange={(event) => setForm({ ...form, confirm_method: event.target.value })}
        >
          <option value="barcode">扫码</option>
          <option value="manual">人工核对</option>
          <option value="signature">签字</option>
        </select>
      </Field>
      <Field label="备注">
        <textarea rows={2} value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} />
      </Field>
      {transfer.error ? <div className="note bad">{transfer.error.message}</div> : null}
    </Modal>
  );
}
