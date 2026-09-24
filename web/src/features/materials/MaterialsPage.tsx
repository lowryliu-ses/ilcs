import { useState } from 'react';

import { api } from '../../shared/api';
import { clock, num } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import type { LotLedger, LotRow, ReservationRow, WasteRow } from '../../shared/types';
import {
  Balances, Bar, ConfirmDialog, Empty, Field, ListState, Modal, NumberInput, Panel, Pill,
  useToast,
} from '../../shared/ui';

export function MaterialsPage() {
  const { can } = useSession();
  const toast = useToast();
  const { sign } = useSignature();
  const lots = useQuery<LotRow[]>('lots', () => api.get<LotRow[]>('/lots'));
  const reservations = useQuery<ReservationRow[]>('reservations', () => api.get<ReservationRow[]>('/reservations'));
  const waste = useQuery<WasteRow[]>('waste', () => api.get<WasteRow[]>('/waste'));
  const [receiving, setReceiving] = useState(false);
  const [editingLot, setEditingLot] = useState<LotRow | null>(null);
  const [deletingLot, setDeletingLot] = useState<LotRow | null>(null);
  const [scrapping, setScrapping] = useState<LotRow | null>(null);
  const [adjusting, setAdjusting] = useState<LotRow | null>(null);
  const [ledgerLot, setLedgerLot] = useState<LotRow | null>(null);
  const [addingTank, setAddingTank] = useState(false);
  const [editingTank, setEditingTank] = useState<WasteRow | null>(null);
  const [deletingTank, setDeletingTank] = useState<WasteRow | null>(null);

  const invalidates = ['lots', 'reservations', 'waste', 'dashboard', 'alarms', 'plans', 'audit'];
  const release = useMutation(
    (payload: { lotId: string; signatureId: string }) =>
      api.post(`/lots/${payload.lotId}/release`, { signature_id: payload.signatureId }),
    { invalidates, onSuccess: () => toast.push('批号已放行，可用于预留') },
  );
  const swap = useMutation((tankId: string) => api.post(`/waste/${tankId}/swap`), {
    invalidates,
    onSuccess: () => toast.push('已记录换桶，设备侧条件恢复'),
  });

  const releaseLot = async (lot: LotRow) => {
    const signatureId = await sign('批号放行', lot.id, ['复验合格', 'SDS 与兼容性已确认']);
    if (!signatureId) return;
    await release.run({ lotId: lot.id, signatureId }).catch((error) => toast.push(error.message));
  };

  const removeLot = useMutation((lotId: string) => api.remove(`/lots/${lotId}`), {
    invalidates,
    onSuccess: () => {
      toast.push('批号已删除');
      setDeletingLot(null);
    },
  });

  const scrapLot = useMutation(
    (payload: { id: string; reason: string; signature_id: string }) =>
      api.post(`/lots/${payload.id}/scrap`, { reason: payload.reason, signature_id: payload.signature_id }),
    {
      invalidates,
      onSuccess: () => {
        toast.push('批号已报废，库存清零；历史投料记录保留');
        setScrapping(null);
      },
    },
  );

  const removeTank = useMutation((tankId: string) => api.remove(`/waste/${tankId}`), {
    invalidates,
    onSuccess: () => {
      toast.push('废液桶登记已移除');
      setDeletingTank(null);
    },
  });

  const confirmScrap = async (lot: LotRow, reason: string) => {
    const signatureId = await sign('批号报废', lot.id, ['质量判定', '已隔离处置']);
    if (!signatureId) return;
    await scrapLot.run({ id: lot.id, reason, signature_id: signatureId }).catch(() => undefined);
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>试剂耗材</h1>
        <span className="small muted">
          账面、未耗用占用、可用量三个数分列。数量只能通过库存事件或盘点调整变化，流水只追加。
        </span>
        {can('material.edit') ? (
          <button className="btn primary" onClick={() => setReceiving(true)}>
            入库登记
          </button>
        ) : null}
      </div>

      <Panel title="批号库存" flush>
        <table>
          <thead>
            <tr>
              <th>批号</th>
              <th>物料</th>
              <th>数量</th>
              <th>放行</th>
              <th>有效截止</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(lots.data ?? []).map((lot) => (
              <tr key={lot.id} className={lot.state === 'scrapped' ? 'retired-row' : undefined}>
                <td className="mono">
                  {lot.id}
                  <div className="tiny muted">{lot.storage}</div>
                </td>
                <td>
                  {lot.material}
                  <div className="tiny muted">
                    {lot.type} · CAS {lot.cas} · {lot.ghs.join('、')}
                  </div>
                </td>
                <td>
                  <Balances
                    balance={lot.qty}
                    outstanding={lot.outstanding}
                    available={lot.available}
                    issued={lot.issued_outstanding}
                    unit={lot.unit}
                  />
                  {lot.use_blockers.length ? (
                    <div className="tiny bad-text">{lot.use_blockers.join('；')}</div>
                  ) : null}
                </td>
                <td>
                  {lot.state === 'scrapped' ? (
                    <>
                      <Pill state="aborted" label="已报废" />
                      <div className="tiny muted">{lot.scrap_reason}</div>
                    </>
                  ) : (
                    <Pill state={lot.release === '已放行' ? 'running' : 'paused'} label={lot.release} />
                  )}
                </td>
                <td className={`small mono ${lot.expired ? 'bad-text' : lot.expiring_soon ? 'warn-text' : ''}`}>
                  {lot.effective_expiry || '未录入'}
                  <div className="tiny muted">依据 {lot.effective_expiry_basis}</div>
                  {lot.open_expiry ? (
                    <div className="tiny muted">开封 {lot.opened} → {lot.open_expiry}</div>
                  ) : null}
                </td>
                <td className="row-end">
                  {lot.release !== '已放行' && lot.state === 'active' && can('material.release') ? (
                    <button className="btn sm" onClick={() => releaseLot(lot)}>
                      放行
                    </button>
                  ) : null}
                  {can('material.edit') && lot.editable_fields.length ? (
                    <button className="btn sm" onClick={() => setEditingLot(lot)}>
                      编辑
                    </button>
                  ) : null}
                  {can('material.edit') && lot.state === 'active' ? (
                    <button className="btn sm" title="实物与账面对不上时用它，差额写审计与流水" onClick={() => setAdjusting(lot)}>
                      盘点
                    </button>
                  ) : null}
                  <button className="btn sm" onClick={() => setLedgerLot(lot)}>
                    流水
                  </button>
                  {can('material.release') && lot.state === 'active' ? (
                    <button className="btn sm danger" title="库存清零、不可再预留，记录保留" onClick={() => setScrapping(lot)}>
                      报废
                    </button>
                  ) : null}
                  {can('material.edit') ? (
                    <button
                      className="btn sm danger"
                      disabled={lot.delete_blockers.length > 0}
                      title={lot.delete_blockers.join('；') || '从未被预留，可直接删除'}
                      onClick={() => setDeletingLot(lot)}
                    >
                      删除
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="panel-body small muted">
          账面库存 = 组织仍持有且未消耗的总量（含已领用未消耗）；未耗用占用 = 授权预留 − 已消耗 −
          已核销损耗 − 已释放；可用量 = 账面库存 − 全部未耗用占用。
          未放行、过期或开封超期的批号不可预留，开跑检查会拦截。
        </div>
      </Panel>

      <Panel title="按批次的预留明细" flush>
        {reservations.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>批次</th>
                <th>批号</th>
                <th>物料</th>
                <th className="num">授权预留</th>
                <th className="num">已消耗</th>
                <th className="num">损耗</th>
                <th className="num">已领未耗</th>
                <th className="num">剩余占用</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {reservations.data.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.batch_id}</td>
                  <td className="mono">{row.lot_id}</td>
                  <td>{row.material}</td>
                  <td className="num mono">
                    {row.qty} {row.unit}
                  </td>
                  <td className="num mono">{row.consumed_qty}</td>
                  <td className="num mono">{row.loss_qty}</td>
                  <td className="num mono">{row.issued_outstanding}</td>
                  <td className="num mono">
                    <b>{row.outstanding}</b>
                  </td>
                  <td>
                    <Pill
                      state={row.state === 'consumed' ? 'done' : row.state === 'released' ? 'aborted' : 'scheduled'}
                      label={{ reserved: '预留', consumed: '已消耗', released: '已释放' }[row.state] ?? row.state}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>还没有预留记录</Empty>
        )}
      </Panel>

      <Panel
        title="废液桶"
        aside={
          can('material.edit') ? (
            <button className="btn sm" onClick={() => setAddingTank(true)}>
              登记废液桶
            </button>
          ) : null
        }
        flush
      >
        <table>
          <thead>
            <tr>
              <th>桶</th>
              <th>类别</th>
              <th>液位</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(waste.data ?? []).map((tank) => (
              <tr key={tank.id}>
                <td className="mono">{tank.id}</td>
                <td>{tank.kind}</td>
                <td>
                  <Bar value={tank.level_pct} max={100} danger={tank.over_threshold} />
                  <span className={`small ${tank.over_threshold ? 'bad-text' : 'muted'}`}>
                    {tank.level_pct}% / {tank.capacity_l} L
                  </span>
                </td>
                <td className="row-end">
                  {can('material.edit') ? (
                    <>
                      <button className="btn sm" onClick={() => swap.run(tank.id).catch((error) => toast.push(error.message))}>
                        AGV 换桶
                      </button>
                      <button className="btn sm" onClick={() => setEditingTank(tank)}>
                        编辑
                      </button>
                      <button
                        className="btn sm danger"
                        disabled={tank.delete_blockers.length > 0}
                        title={tank.delete_blockers.join('；') || '空桶可移除登记'}
                        onClick={() => setDeletingTank(tank)}
                      >
                        移除
                      </button>
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {receiving ? <ReceiveDialog onClose={() => setReceiving(false)} invalidates={invalidates} /> : null}
      {editingLot ? <LotEditDialog lot={editingLot} onClose={() => setEditingLot(null)} invalidates={invalidates} /> : null}
      {adjusting ? <LotAdjustDialog lot={adjusting} onClose={() => setAdjusting(null)} invalidates={invalidates} /> : null}
      {ledgerLot ? <LedgerDialog lot={ledgerLot} onClose={() => setLedgerLot(null)} /> : null}
      {addingTank ? <TankDialog onClose={() => setAddingTank(false)} invalidates={invalidates} /> : null}
      {editingTank ? <TankDialog tank={editingTank} onClose={() => setEditingTank(null)} invalidates={invalidates} /> : null}

      {deletingLot ? (
        <ConfirmDialog
          title={`删除批号 · ${deletingLot.id}`}
          danger
          confirmLabel="删除"
          pending={removeLot.pending}
          error={removeLot.error?.message}
          onClose={() => setDeletingLot(null)}
          onConfirm={() => removeLot.run(deletingLot.id).catch(() => undefined)}
        >
          <div className="note warn">
            {deletingLot.material} <span className="mono">{deletingLot.qty}</span> {deletingLot.unit}
            从未被任何批次预留、也没有投料流水，可以直接删除。
            用过的批号只能报废——它进过某个批次的投料记录。
          </div>
        </ConfirmDialog>
      ) : null}

      {scrapping ? (
        <ConfirmDialog
          title={`批号报废 · ${scrapping.id}`}
          danger
          confirmLabel="签名并报废"
          reasonLabel="报废理由"
          reasonPlaceholder="例如：开封后吸潮，水分超标"
          pending={scrapLot.pending}
          error={scrapLot.error?.message}
          onClose={() => setScrapping(null)}
          onConfirm={(reason) => confirmScrap(scrapping, reason)}
        >
          <div className="note warn">
            报废后库存清零、不可再预留，但记录保留：它可能已经出现在某个批次的投料记录里。
            当前账面 <span className="mono">{scrapping.qty}</span> {scrapping.unit}，未耗用占用{' '}
            <span className="mono">{scrapping.outstanding}</span>。报废会写一条损耗流水，库存不会凭状态字段无声消失。
          </div>
          <div className="small muted">有未消耗预留时服务端会拒绝，先终止相关批次释放预留。</div>
        </ConfirmDialog>
      ) : null}

      {deletingTank ? (
        <ConfirmDialog
          title={`移除废液桶登记 · ${deletingTank.id}`}
          danger
          confirmLabel="移除"
          pending={removeTank.pending}
          error={removeTank.error?.message}
          onClose={() => setDeletingTank(null)}
          onConfirm={() => removeTank.run(deletingTank.id).catch(() => undefined)}
        >
          <div className="note warn">液位为 0，现场无实物，可以从台账移除。</div>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

/* 改批号信息。可改哪些字段由服务端算好放在 editable_fields 里——已放行的批号，
   数量和有效期进过质量判断，只剩存放位置这类事务性信息还能维护。 */
function LotEditDialog({ lot, onClose, invalidates }: { lot: LotRow; onClose: () => void; invalidates: string[] }) {
  const toast = useToast();
  const [form, setForm] = useState<Record<string, string | number>>(() => ({
    material: lot.material, cas: lot.cas, type: lot.type, qty: lot.qty, unit: lot.unit,
    expiry: lot.expiry, storage: lot.storage, sds: lot.sds, compat: lot.compat, opened: lot.opened,
  }));
  const [error, setError] = useState('');
  const editable = new Set(lot.editable_fields);

  const save = useMutation((payload: Record<string, unknown>) => api.patch(`/lots/${lot.id}`, payload), {
    invalidates,
    onSuccess: () => {
      toast.push('批号信息已更新');
      onClose();
    },
  });

  const submit = () => {
    const payload = Object.fromEntries(Object.entries(form).filter(([key]) => editable.has(key)));
    save.run(payload).catch((caught) => setError(caught.message));
  };

  const text = (key: string, label: string, hint?: string) => (
    <Field label={label} hint={editable.has(key) ? hint : '当前状态下不可修改'}>
      <input
        value={String(form[key] ?? '')}
        disabled={!editable.has(key)}
        onChange={(e) => setForm((c) => ({ ...c, [key]: e.target.value }))}
      />
    </Field>
  );

  return (
    <Modal
      title={`编辑批号 · ${lot.id}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={save.pending} onClick={submit}>保存</button>
        </>
      }
    >
      <div className={lot.release === '已放行' ? 'note warn' : 'note'}>
        {lot.release === '已放行'
          ? '已放行：数量、有效期、CAS 这些进过放行判断的字段不能再改。数量偏差请用「盘点」，质量问题请用「报废」。'
          : '未放行：录入信息都可以改。放行之后只剩存放位置、SDS 这类事务性字段。'}
      </div>
      <div className="grid cols-2">
        {text('material', '物料')}
        {text('cas', 'CAS')}
      </div>
      <div className="grid cols-3">
        <Field label="数量" hint={editable.has('qty') ? undefined : '已放行，请用盘点调整'}>
          <NumberInput
            value={Number(form.qty)}
            disabled={!editable.has('qty')}
            ariaLabel="数量"
            onChange={(v) => setForm((c) => ({ ...c, qty: v === '' ? 0 : v }))}
          />
        </Field>
        {text('unit', '单位')}
        {text('expiry', '有效期')}
      </div>
      <div className="grid cols-2">
        {text('storage', '存放位置')}
        {text('opened', '开封日期')}
      </div>
      <div className="grid cols-2">
        {text('sds', 'SDS')}
        {text('compat', '兼容性组')}
      </div>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/* 盘点调整。差额与理由都写审计；不能调到低于已预留量，否则在途批次会凭空缺料。 */
function LotAdjustDialog({ lot, onClose, invalidates }: { lot: LotRow; onClose: () => void; invalidates: string[] }) {
  const toast = useToast();
  // 服务端的数量是十进制字符串；这里只在界面上做差额提示，提交仍原样发字符串
  const balance = Number(lot.qty);
  const outstanding = Number(lot.outstanding);
  const [qty, setQty] = useState<number | ''>(balance);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');

  const adjust = useMutation(
    (payload: { qty: string; reason: string }) => api.post(`/lots/${lot.id}/adjust`, payload),
    {
      invalidates,
      onSuccess: () => {
        toast.push('盘点调整已记录，并写入库存流水');
        onClose();
      },
    },
  );

  const delta = qty === '' ? 0 : qty - balance;
  const belowReserved = qty !== '' && qty < outstanding;

  return (
    <Modal
      title={`盘点调整 · ${lot.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button
            className="btn primary"
            disabled={adjust.pending || qty === '' || !reason.trim() || belowReserved}
            onClick={() => adjust.run({ qty: String(qty), reason: reason.trim() }).catch((c) => setError(c.message))}
          >
            保存调整
          </button>
        </>
      }
    >
      <div className="note">
        账面 <span className="mono">{lot.qty}</span> {lot.unit}，其中未耗用占用{' '}
        <span className="mono">{lot.outstanding}</span>。盘点值不能低于未耗用占用——在途批次靠它算可用量。
      </div>
      <Field label={`盘点实测数量（${lot.unit}）`}>
        <NumberInput value={qty} invalid={belowReserved} ariaLabel="盘点数量" onChange={setQty} />
      </Field>
      <div className={`small ${belowReserved ? 'bad-text' : 'muted'}`}>
        {belowReserved
          ? `低于未耗用占用 ${lot.outstanding} ${lot.unit}，先终止相关批次释放预留`
          : `差额 ${delta >= 0 ? '+' : ''}${num(delta, 3)} ${lot.unit}`}
      </div>
      <Field label="理由" hint="写入审计记录">
        <textarea rows={2} value={reason} placeholder="例如：季度盘点，与台账差 8 mL" onChange={(e) => setReason(e.target.value)} />
      </Field>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

/* 废液桶登记与编辑。同一个表单兼顾新增与修改：字段完全一样。 */
function TankDialog({ tank, onClose, invalidates }: { tank?: WasteRow; onClose: () => void; invalidates: string[] }) {
  const toast = useToast();
  const [form, setForm] = useState({
    id: tank?.id ?? 'WT-', kind: tank?.kind ?? '', capacity_l: tank?.capacity_l ?? 20,
    level_pct: tank?.level_pct ?? 0,
  });
  const [error, setError] = useState('');

  const save = useMutation(
    (payload: Record<string, unknown>) =>
      tank ? api.patch(`/waste/${tank.id}`, payload) : api.post('/waste', payload),
    {
      invalidates,
      onSuccess: () => {
        toast.push(tank ? '废液桶已更新' : '废液桶已登记');
        onClose();
      },
    },
  );

  const submit = () => {
    const payload = tank
      ? { kind: form.kind, capacity_l: form.capacity_l, level_pct: form.level_pct }
      : form;
    save.run(payload).catch((caught) => setError(caught.message));
  };

  return (
    <Modal
      title={tank ? `编辑废液桶 · ${tank.id}` : '登记废液桶'}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={save.pending || !form.kind.trim()} onClick={submit}>
            {tank ? '保存' : '登记'}
          </button>
        </>
      }
    >
      {tank ? null : (
        <Field label="桶号" hint="例如 WT-03，登记后不可更改">
          <input className="mono" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} />
        </Field>
      )}
      <Field label="类别">
        <input value={form.kind} placeholder="例如：有机废液 / 水系废液" onChange={(e) => setForm({ ...form, kind: e.target.value })} />
      </Field>
      <div className="grid cols-2">
        <Field label="容量 L">
          <NumberInput value={form.capacity_l} invalid={!(form.capacity_l > 0)} ariaLabel="容量"
            onChange={(v) => setForm({ ...form, capacity_l: Number(v) || 0 })} />
        </Field>
        <Field label="当前液位 %" hint="超过 75% 会在工作台提醒">
          <NumberInput value={form.level_pct} invalid={form.level_pct < 0 || form.level_pct > 100} ariaLabel="液位"
            onChange={(v) => setForm({ ...form, level_pct: Number(v) || 0 })} />
        </Field>
      </div>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}

function ReceiveDialog({ onClose, invalidates }: { onClose: () => void; invalidates: string[] }) {
  const toast = useToast();
  const [form, setForm] = useState({
    id: '',
    material: '',
    cas: '',
    type: '',
    qty: 1,
    unit: 'g',
    expiry: '',
    storage: '',
  });
  const receive = useMutation(() => api.post('/lots', form), {
    invalidates,
    onSuccess: () => {
      toast.push('已入库，状态为待复验；放行需 QA 或 EHS 签名');
      onClose();
    },
  });

  return (
    <Modal
      title="入库登记"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!form.id || !form.material || !form.expiry || receive.pending}
            onClick={() => receive.run().catch(() => undefined)}
          >
            登记（待复验）
          </button>
        </>
      }
    >
      <div className="grid cols-2">
        <Field label="批号">
          <input value={form.id} onChange={(event) => setForm({ ...form, id: event.target.value })} />
        </Field>
        <Field label="物料名称">
          <input value={form.material} onChange={(event) => setForm({ ...form, material: event.target.value })} />
        </Field>
        <Field label="CAS">
          <input value={form.cas} onChange={(event) => setForm({ ...form, cas: event.target.value })} />
        </Field>
        <Field label="类别">
          <input value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value })} />
        </Field>
        <Field label="数量">
          <input
            type="number"
            value={form.qty}
            onChange={(event) => setForm({ ...form, qty: Number(event.target.value) })}
          />
        </Field>
        <Field label="单位" hint="必须与流程 BOM 的单位一致才能预留">
          <input value={form.unit} onChange={(event) => setForm({ ...form, unit: event.target.value })} />
        </Field>
        <Field label="有效期">
          <input type="date" value={form.expiry} onChange={(event) => setForm({ ...form, expiry: event.target.value })} />
        </Field>
        <Field label="存储条件">
          <input value={form.storage} onChange={(event) => setForm({ ...form, storage: event.target.value })} />
        </Field>
      </div>
      {receive.error ? <div className="note bad">{receive.error.message}</div> : null}
    </Modal>
  );
}

/** 批号流水与对账。账面、占用、可用三个量与流水累计放在一起，账实差额一眼能看见。 */
function LedgerDialog({ lot, onClose }: { lot: LotRow; onClose: () => void }) {
  const ledger = useQuery<LotLedger>(`lots:${lot.id}:ledger`, () =>
    api.get<LotLedger>(`/lots/${lot.id}/ledger`),
  );
  const rows = ledger.data;

  return (
    <Modal title={`库存流水 · ${lot.id}`} onClose={onClose} wide>
      {rows ? (
        <>
          <div className="note">
            <Balances
              balance={rows.balance}
              outstanding={rows.outstanding}
              available={rows.available}
              issued={rows.issued_outstanding}
              unit={rows.unit}
            />
            <div className="small">
              期初 <span className="mono">{rows.opening_balance}</span> · 流水累计{' '}
              <span className="mono">{rows.ledger_sum}</span>
              {rows.reconciled ? (
                <span className="tag"> 已对平</span>
              ) : (
                <span className="tag bad"> 账实差额，需核查</span>
              )}
            </div>
          </div>
          {rows.lines.length ? (
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>事件</th>
                  <th>来源 / 编号</th>
                  <th className="num">数量</th>
                  <th className="num">余额变化</th>
                  <th className="num">余额</th>
                  <th>关联</th>
                </tr>
              </thead>
              <tbody>
                {rows.lines.map((line) => (
                  <tr key={line.id}>
                    <td className="small">{clock(line.created_at)}</td>
                    <td className="small">
                      {line.event_label}
                      {line.note ? <div className="tiny muted">{line.note}</div> : null}
                    </td>
                    <td className="tiny mono">
                      {line.source}/{line.event_id}#{line.line_no}
                    </td>
                    <td className="num mono">{line.quantity}</td>
                    <td className="num mono">{line.balance_delta}</td>
                    <td className="num mono">{line.balance_after}</td>
                    <td className="tiny mono">
                      {line.batch_id || '—'}
                      {line.reservation_id ? ` · 预留 ${line.reservation_id}` : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>还没有流水记录</Empty>
          )}
          <div className="panel-body small muted">
            流水只追加。记错的记录通过受控冲正与更正记录修复，不直接改已入账的行。
          </div>
        </>
      ) : (
        <ListState loading={ledger.loading} error={ledger.error} />
      )}
    </Modal>
  );
}
