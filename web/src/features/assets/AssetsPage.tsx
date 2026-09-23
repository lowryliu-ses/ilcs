import { useState } from 'react';

import { api, pageQuery } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { AssetRow, BookingRow, CapabilityRow, Paged, StationRow } from '../../shared/types';
import {
  Blocked, ConfirmDialog, Empty, Field, FileUpload, ListState, Modal, Pager, Panel, Pill, useToast,
} from '../../shared/ui';

export function AssetsPage() {
  const { can } = useSession();
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState<AssetRow | null>(null);
  const [booking, setBooking] = useState(false);
  const [cancelling, setCancelling] = useState<BookingRow | null>(null);

  const query = pageQuery({ page, page_size: 20, keyword });
  const assets = useQuery<Paged<AssetRow>>(`assets:${query}`, () => api.get<Paged<AssetRow>>(`/assets${query}`));
  const bookings = useQuery<BookingRow[]>(
    'assets:bookings', () => api.get<BookingRow[]>('/resource-bookings'), 30000,
  );

  const cancelBooking = useMutation(
    (payload: { id: string; reason: string }) =>
      api.post(`/resource-bookings/${payload.id}/cancel`, { reason: payload.reason }),
    {
      invalidates: ['assets', 'schedule', 'dashboard', 'audit'],
      onSuccess: () => {
        toast.push('占用已取消');
        setCancelling(null);
      },
    },
  );

  const rows = assets.data?.items ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>仪器设备</h1>
        <span className="small muted">
          容量按资产算：一台资产映射多个工位时共享同一份容量与校准许可，不因工位 ID 不同就重复占用。
        </span>
      </div>

      <div className="split">
        <Panel
          title={`资产（${assets.data?.total ?? 0}）`}
          aside={
            <div className="filters">
              <input
                placeholder="资产号、名称或序列号"
                value={keyword}
                onChange={(event) => {
                  setKeyword(event.target.value);
                  setPage(1);
                }}
              />
              {can('asset.edit') ? (
                <button className="btn primary sm" onClick={() => setCreating(true)}>
                  登记资产
                </button>
              ) : null}
            </div>
          }
          flush
        >
          <ListState
            loading={assets.loading && !assets.data}
            error={assets.error}
            empty={!rows.length}
            emptyText="没有符合条件的资产"
          />
          {rows.length ? (
            <table>
              <thead>
                <tr>
                  <th>资产号</th>
                  <th>名称</th>
                  <th>工位</th>
                  <th>容量</th>
                  <th>校准</th>
                  <th>不可用原因</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">{row.asset_no}</td>
                    <td>
                      <b>{row.name}</b>
                      <div className="tiny muted">
                        {row.model || '—'} · {row.location || '—'}
                      </div>
                    </td>
                    <td className="small mono">{row.station_ids.join('、') || '无'}</td>
                    <td className="mono small">{row.capacity}</td>
                    <td className="small">
                      {row.calibration_applicable ? (
                        <>
                          <Pill state={row.calibration_valid ? 'valid' : 'expired'} label={row.calibration_valid ? '有效' : '缺失或过期'} />
                          {row.calibration_due ? (
                            <div className="tiny muted">至 {clock(row.calibration_due)}</div>
                          ) : null}
                        </>
                      ) : (
                        <span className="tag" title={row.calibration_exempt_reason}>
                          不适用校准
                        </span>
                      )}
                    </td>
                    <td className="small">
                      {row.unavailable_reasons.length ? (
                        <Blocked reasons={row.unavailable_reasons} />
                      ) : (
                        <span className="muted">可用</span>
                      )}
                    </td>
                    <td className="row-end">
                      {can('asset.edit') ? (
                        <button className="btn sm" onClick={() => setEditing(row)}>
                          编辑
                        </button>
                      ) : null}
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
            page={assets.data?.page ?? 1}
            pageSize={assets.data?.page_size ?? 20}
            total={assets.data?.total ?? 0}
            onChange={setPage}
          />
        </Panel>

        <Panel
          title={`资源占用（${bookings.data?.length ?? 0}）`}
          aside={
            can('booking.edit') ? (
              <button className="btn primary sm" onClick={() => setBooking(true)}>
                新建预约
              </button>
            ) : null
          }
          flush
        >
          {bookings.data?.length ? (
            <table>
              <tbody>
                {bookings.data.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <span className="tag">{row.kind_label}</span> <b>{row.asset_name}</b>
                      <div className="tiny muted">{row.reason || '—'}</div>
                    </td>
                    <td className="small">
                      {clock(row.starts_at)}
                      <div className="tiny muted">至 {clock(row.ends_at)}</div>
                    </td>
                    <td>
                      <Pill state={row.state} />
                    </td>
                    <td className="row-end">
                      {can('booking.edit') && row.state === 'confirmed' ? (
                        <button
                          className="btn sm"
                          disabled={cancelBooking.pending}
                          onClick={() => setCancelling(row)}
                        >
                          取消
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>没有生效的资源占用</Empty>
          )}
        </Panel>
      </div>

      {creating ? <CreateDialog onClose={() => setCreating(false)} /> : null}
      {editing ? <EditDialog asset={editing} onClose={() => setEditing(null)} /> : null}
      {cancelling ? (
        <ConfirmDialog
          title={`取消占用 · ${cancelling.asset_name}`}
          confirmLabel="取消占用"
          reasonLabel="取消原因"
          pending={cancelBooking.pending}
          error={cancelBooking.error?.message}
          onConfirm={(reason) => cancelBooking.run({ id: cancelling.id, reason })}
          onClose={() => setCancelling(null)}
        >
          取消只释放这一段占用。已经按它排下去的工步不会自动重排，需要到排程页确认。
        </ConfirmDialog>
      ) : null}
      {selected ? <DetailDialog assetId={selected} onClose={() => setSelected(null)} /> : null}
      {booking ? <BookingDialog assets={rows} onClose={() => setBooking(false)} /> : null}
    </div>
  );
}

function DetailDialog({ assetId, onClose }: { assetId: string; onClose: () => void }) {
  const { can } = useSession();
  const toast = useToast();
  const detail = useQuery<AssetRow>(`assets:${assetId}`, () => api.get<AssetRow>(`/assets/${assetId}`));
  const stations = useQuery<StationRow[]>('stations', () => api.get<StationRow[]>('/stations'));
  const [calibrating, setCalibrating] = useState(false);
  const [linking, setLinking] = useState('');

  const link = useMutation(
    () => api.post(`/assets/${assetId}/stations`, { station_id: linking }),
    {
      invalidates: ['assets', 'stations'],
      onSuccess: () => {
        toast.push('工位已关联，容量与校准许可随资产共享');
        setLinking('');
      },
    },
  );

  const asset = detail.data;

  return (
    <Modal title={`资产 · ${asset?.asset_no ?? assetId}`} onClose={onClose} wide>
      {asset ? (
        <>
          <div className="metrics">
            <div className="metric">
              <span className="metric-label">状态</span>
              <strong className="metric-value">
                <Pill state={asset.state} />
              </strong>
              <span className="metric-hint">容量 {asset.capacity}</span>
            </div>
            <div className="metric">
              <span className="metric-label">序列号</span>
              <strong className="metric-value mono" style={{ fontSize: 14 }}>
                {asset.serial || '—'}
              </strong>
              <span className="metric-hint">设备心跳会拿它与这里核对</span>
            </div>
            <div className="metric">
              <span className="metric-label">责任人</span>
              <strong className="metric-value" style={{ fontSize: 14 }}>
                {asset.owner_name || '—'}
              </strong>
              <span className="metric-hint">{asset.location || '—'}</span>
            </div>
          </div>

          {asset.unavailable_reasons.length ? (
            <div className="note warn">
              当前不可用：
              <Blocked reasons={asset.unavailable_reasons} />
            </div>
          ) : null}

          <Panel
            title="校准记录"
            aside={
              can('asset.edit') ? (
                <button className="btn sm" onClick={() => setCalibrating(true)}>
                  登记校准
                </button>
              ) : null
            }
            flush
          >
            {asset.calibrations?.length ? (
              <table>
                <thead>
                  <tr>
                    <th>范围</th>
                    <th>结果</th>
                    <th>生效</th>
                    <th>到期</th>
                    <th>证书</th>
                  </tr>
                </thead>
                <tbody>
                  {asset.calibrations.map((row) => (
                    <tr key={row.id}>
                      <td className="small">{row.capability_scope.join('、') || '整台资产'}</td>
                      <td>
                        <Pill state={row.result === 'pass' ? 'valid' : 'invalid'} label={row.result === 'pass' ? '合格' : '不合格'} />
                        {row.valid_now ? <div className="tiny">当前有效</div> : <div className="tiny muted">当前无效</div>}
                      </td>
                      <td className="small">{clock(row.effective_from)}</td>
                      <td className="small">{row.expires_at ? clock(row.expires_at) : '未设'}</td>
                      <td className="small">
                        {row.certificate_file_id ? (
                          <button
                            className="btn sm"
                            onClick={() =>
                              api
                                .download(`/files/${row.certificate_file_id}/download`, `${asset.asset_no}-cal.pdf`)
                                .catch((error) => toast.push(error.message))
                            }
                          >
                            下载
                          </button>
                        ) : (
                          <span className="muted">无</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>
                {asset.calibration_applicable
                  ? '没有校准记录：需要校准的设备步骤会被开跑检查拦住'
                  : `已明确不适用校准：${asset.calibration_exempt_reason}`}
              </Empty>
            )}
          </Panel>

          <Panel title="关联工位" flush>
            <div className="filters" style={{ padding: '10px 14px' }}>
              <select value={linking} onChange={(event) => setLinking(event.target.value)}>
                <option value="">选择工位</option>
                {(stations.data ?? [])
                  .filter((row) => !asset.station_ids.includes(row.id))
                  .map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.id} · {row.name}
                    </option>
                  ))}
              </select>
              <button
                className="btn sm"
                disabled={!linking || link.pending || !can('asset.edit')}
                onClick={() => link.run().catch((error) => toast.push(error.message))}
              >
                关联
              </button>
            </div>
            {asset.station_ids.length ? (
              <table>
                <tbody>
                  {asset.station_ids.map((id) => (
                    <tr key={id}>
                      <td className="mono">{id}</td>
                      <td className="small muted">共享该资产的容量与校准许可</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>没有关联工位（无适配器的仪器或手工工作台可以这样）</Empty>
            )}
          </Panel>

          <Panel title="占用记录" flush>
            {asset.bookings?.length ? (
              <table>
                <tbody>
                  {asset.bookings.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <span className="tag">{row.kind_label}</span> {row.reason || '—'}
                      </td>
                      <td className="small">
                        {clock(row.starts_at)} → {clock(row.ends_at)}
                      </td>
                      <td>
                        <Pill state={row.state} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>没有占用记录</Empty>
            )}
          </Panel>
        </>
      ) : (
        <ListState loading={detail.loading} error={detail.error} />
      )}

      {calibrating && asset ? (
        <CalibrationDialog asset={asset} onClose={() => setCalibrating(false)} />
      ) : null}
    </Modal>
  );
}

function CalibrationDialog({ asset, onClose }: { asset: AssetRow; onClose: () => void }) {
  const toast = useToast();
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const [result, setResult] = useState<'pass' | 'fail'>('pass');
  const [scope, setScope] = useState<string[]>([]);
  const [expires, setExpires] = useState('');
  const [note, setNote] = useState('');
  const [file, setFile] = useState<{ id: string; filename: string } | null>(null);

  const upload = useMutation(
    (picked: File) => api.upload<{ id: string; filename: string }>('/files', picked, { ref_type: 'calibration' }),
    { onSuccess: (row) => setFile(row) },
  );
  const create = useMutation(
    () =>
      api.post(`/assets/${asset.id}/calibrations`, {
        result,
        capability_scope: scope,
        expires_at: expires ? new Date(expires).toISOString() : null,
        certificate_file_id: file?.id ?? '',
        note,
      }),
    {
      invalidates: ['assets', 'stations', 'batches'],
      onSuccess: () => {
        toast.push('校准记录已登记');
        onClose();
      },
    },
  );

  return (
    <Modal
      title={`登记校准 · ${asset.asset_no}`}
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
        只有「有效且合格」的记录构成许可。开跑检查会按每个设备步骤的完整执行区间校验——
        区间内到期同样算不通过。
      </div>
      <Field label="结果">
        <select value={result} onChange={(event) => setResult(event.target.value as typeof result)}>
          <option value="pass">合格</option>
          <option value="fail">不合格</option>
        </select>
      </Field>
      <Field label="适用范围" hint="不选表示覆盖整台资产">
        <select
          multiple
          size={4}
          value={scope}
          onChange={(event) => setScope(Array.from(event.target.selectedOptions).map((option) => option.value))}
        >
          {(capabilities.data ?? []).map((row) => (
            <option key={row.id} value={row.id}>
              {row.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="到期时间">
        <input type="datetime-local" value={expires} onChange={(event) => setExpires(event.target.value)} />
      </Field>
      <FileUpload
        label="校准证书"
        accept="application/pdf,image/png,image/jpeg"
        pending={upload.pending}
        onPick={(picked) => upload.run(picked).catch((error) => toast.push(error.message))}
      />
      {file ? <div className="note">已上传 {file.filename}</div> : null}
      <Field label="备注">
        <textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
      </Field>
      {upload.error ? <div className="note bad">{upload.error.message}</div> : null}
      {create.error ? <div className="note bad">{create.error.message}</div> : null}
    </Modal>
  );
}

function BookingDialog({ assets, onClose }: { assets: AssetRow[]; onClose: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({ asset_id: '', kind: 'maintenance', reason: '' });
  const [starts, setStarts] = useState('');
  const [ends, setEnds] = useState('');
  const [impacted, setImpacted] = useState<{ batch_id: string; step_index: number; action: string }[]>([]);

  const create = useMutation(
    () =>
      api.post('/resource-bookings', {
        ...form,
        starts_at: new Date(starts).toISOString(),
        ends_at: new Date(ends).toISOString(),
      }),
    {
      invalidates: ['assets', 'schedule', 'batches'],
      onSuccess: (result) => {
        const row = result as { impacted?: typeof impacted };
        setImpacted(row.impacted ?? []);
        toast.push(
          row.impacted?.length
            ? `已创建；${row.impacted.length} 个已排程工步受影响，需操作员确认重排`
            : '已创建',
        );
      },
    },
  );

  return (
    <Modal
      title="新建资源占用"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            关闭
          </button>
          <button
            className="btn primary"
            disabled={!form.asset_id || !starts || !ends || create.pending}
            onClick={() => create.run().catch(() => undefined)}
          >
            创建
          </button>
        </>
      }
    >
      <div className="note">
        维护、人工预约与自动排程共用一套冲突判断；确认时在事务里重新校验，不看提交时界面上的空档。
      </div>
      <Field label="资产">
        <select value={form.asset_id} onChange={(event) => setForm({ ...form, asset_id: event.target.value })}>
          <option value="">选择资产</option>
          {assets.map((row) => (
            <option key={row.id} value={row.id}>
              {row.asset_no} · {row.name}（容量 {row.capacity}）
            </option>
          ))}
        </select>
      </Field>
      <Field label="类型">
        <select value={form.kind} onChange={(event) => setForm({ ...form, kind: event.target.value })}>
          <option value="maintenance">维护</option>
          <option value="manual">人工预约</option>
          <option value="calibration">校准</option>
        </select>
      </Field>
      <div className="grid cols-2">
        <Field label="开始">
          <input type="datetime-local" value={starts} onChange={(event) => setStarts(event.target.value)} />
        </Field>
        <Field label="结束">
          <input type="datetime-local" value={ends} onChange={(event) => setEnds(event.target.value)} />
        </Field>
      </div>
      <Field label="原因">
        <textarea rows={2} value={form.reason} onChange={(event) => setForm({ ...form, reason: event.target.value })} />
      </Field>
      {impacted.length ? (
        <div className="note warn">
          受影响的已排程工步（不会被自动移动）：
          <ul className="blocked">
            {impacted.map((row, index) => (
              <li key={index}>
                {row.batch_id} 第 {row.step_index + 1} 步 · {row.action}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {create.error ? (
        <div className="note bad">
          {create.error.message}
          <Blocked reasons={create.error.blocked.map((row) => row.label)} />
        </div>
      ) : null}
    </Modal>
  );
}

/* 台账与可用性一起改。

   状态与容量不是台账装饰：`maintenance` / `retired` 会让资产退出排程与设备动作，
   容量决定同一台资产能被几个工步同时占用。所以这个表单带乐观并发的 row_version——
   两个人同时改同一台资产时，后提交的那个会收到 409 而不是悄悄覆盖前一个。 */
function EditDialog({ asset, onClose }: { asset: AssetRow; onClose: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({
    name: asset.name,
    model: asset.model,
    serial: asset.serial,
    location: asset.location,
    state: asset.state,
    capacity: asset.capacity,
    calibration_applicable: asset.calibration_applicable,
    calibration_exempt_reason: asset.calibration_exempt_reason,
    note: asset.note,
  });

  const save = useMutation(
    () => api.patch(`/assets/${asset.id}`, { ...form, row_version: asset.row_version }),
    {
      invalidates: ['assets', 'stations', 'schedule', 'dashboard', 'audit'],
      onSuccess: () => {
        toast.push('资产已更新');
        onClose();
      },
    },
  );

  // 不适用校准必须写理由——后端会拒，这里先挡住，省一次往返
  const exemptMissing = !form.calibration_applicable && !form.calibration_exempt_reason.trim();

  return (
    <Modal
      title={`编辑资产 · ${asset.asset_no}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!form.name || exemptMissing || save.pending}
            onClick={() => save.run().catch(() => undefined)}
          >
            保存
          </button>
        </>
      }
    >
      <Field label="名称">
        <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
      </Field>
      <div className="grid cols-2">
        <Field label="型号">
          <input value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} />
        </Field>
        <Field label="序列号">
          <input value={form.serial} onChange={(event) => setForm({ ...form, serial: event.target.value })} />
        </Field>
      </div>
      <Field label="位置">
        <input value={form.location} onChange={(event) => setForm({ ...form, location: event.target.value })} />
      </Field>
      <div className="grid cols-2">
        <Field
          label="状态"
          hint={
            form.state === 'active'
              ? '正常参与排程与设备动作'
              : form.state === 'maintenance'
              ? '维护中：不接新的工步，已排下的需要重排'
              : '已退役：退出排程匹配，历史分配仍指向它'
          }
        >
          <select value={form.state} onChange={(event) => setForm({ ...form, state: event.target.value })}>
            <option value="active">正常</option>
            <option value="maintenance">维护中</option>
            <option value="retired">已退役</option>
          </select>
        </Field>
        <Field label="共享容量" hint="同一台资产能同时承接几个工步；确可独立并行才调大">
          <input
            type="number"
            min={1}
            value={form.capacity}
            onChange={(event) => setForm({ ...form, capacity: Math.max(1, Number(event.target.value)) })}
          />
        </Field>
      </div>
      {asset.station_ids.length > 1 && form.capacity < asset.station_ids.length ? (
        <div className="note">
          这台资产映射了 {asset.station_ids.length} 个工位（{asset.station_ids.join('、')}），
          容量 {form.capacity} 意味着它们不能同时开工——这正是资产级容量的作用，确认这与现场一致。
        </div>
      ) : null}
      <Field label="校准">
        <label className="small">
          <input
            type="checkbox"
            checked={form.calibration_applicable}
            onChange={(event) =>
              setForm({ ...form, calibration_applicable: event.target.checked })
            }
          />
          需要校准
        </label>
      </Field>
      {form.calibration_applicable ? (
        <div className="note">
          合格校准必须附证书文件。要让这台资产可用，请在「详情」里登记一条带证书的校准记录；
          如果它本来就没有计量输出，才改为「不适用校准」并写明理由。
        </div>
      ) : (
        <Field label="不适用校准的理由（必填）" hint="缺失不等同不适用，所以必须写清楚">
          <textarea
            rows={2}
            value={form.calibration_exempt_reason}
            onChange={(event) =>
              setForm({ ...form, calibration_exempt_reason: event.target.value })
            }
          />
        </Field>
      )}
      <Field label="备注">
        <textarea
          rows={2}
          value={form.note}
          onChange={(event) => setForm({ ...form, note: event.target.value })}
        />
      </Field>
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}

function CreateDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({
    asset_no: '', name: '', model: '', serial: '', location: '', capacity: 1,
    calibration_applicable: true, calibration_exempt_reason: '',
  });

  const create = useMutation(() => api.post('/assets', form), {
    invalidates: ['assets'],
    onSuccess: () => {
      toast.push('资产已登记');
      onClose();
    },
  });

  return (
    <Modal
      title="登记资产"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!form.asset_no || !form.name || create.pending}
            onClick={() => create.run().catch(() => undefined)}
          >
            登记
          </button>
        </>
      }
    >
      <div className="note">
        没有适配器的仪器和手工工作台也要登记：它们同样需要预约与占用，只是不接设备指令。
      </div>
      <Field label="资产号">
        <input value={form.asset_no} onChange={(event) => setForm({ ...form, asset_no: event.target.value })} />
      </Field>
      <Field label="名称">
        <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
      </Field>
      <div className="grid cols-2">
        <Field label="型号">
          <input value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} />
        </Field>
        <Field label="序列号">
          <input value={form.serial} onChange={(event) => setForm({ ...form, serial: event.target.value })} />
        </Field>
      </div>
      <Field label="位置">
        <input value={form.location} onChange={(event) => setForm({ ...form, location: event.target.value })} />
      </Field>
      <Field label="共享容量" hint="默认 1 份独占容量；确可独立并行的通道才调大">
        <input
          type="number"
          min={1}
          value={form.capacity}
          onChange={(event) => setForm({ ...form, capacity: Math.max(1, Number(event.target.value)) })}
        />
      </Field>
      <Field label="校准">
        <label className="small">
          <input
            type="checkbox"
            checked={form.calibration_applicable}
            onChange={(event) => setForm({ ...form, calibration_applicable: event.target.checked })}
          />
          需要校准
        </label>
      </Field>
      {form.calibration_applicable ? null : (
        <Field label="不适用校准的理由（必填）" hint="缺失不等同不适用，所以必须写清楚">
          <textarea
            rows={2}
            value={form.calibration_exempt_reason}
            onChange={(event) => setForm({ ...form, calibration_exempt_reason: event.target.value })}
          />
        </Field>
      )}
      {create.error ? <div className="note bad">{create.error.message}</div> : null}
    </Modal>
  );
}
