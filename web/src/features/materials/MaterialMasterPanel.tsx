/* 物料主数据：一种料「叫什么、按什么单位记账、属于哪一类、其他单位怎么折算」。

   类别决定配液模板怎么加这种料（溶剂、锂盐……），单位换算决定入库与设备回报怎么折成基础单位。
   已有批号的物料不能改名称与基础单位——批号、预留与设备回报的消耗按名称与单位对账，由服务端判，
   这里按 locked_fields 置灰。不用的物料停用、不删除：批号与流水都指回它。 */
import { useMemo, useState } from 'react';

import { api } from '../../shared/api';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { MaterialRow } from '../../shared/types';
import { ConfirmDialog, Empty, Field, ListState, Modal, Panel, Pill, useToast } from '../../shared/ui';

const INVALIDATES = ['materials', 'lots', 'audit', 'formulation-templates'];

export function MaterialMasterPanel() {
  const { can } = useSession();
  const toast = useToast();
  const materials = useQuery<MaterialRow[]>('materials', () => api.get<MaterialRow[]>('/materials'));
  const [editing, setEditing] = useState<MaterialRow | 'new' | null>(null);
  const [retiring, setRetiring] = useState<MaterialRow | null>(null);
  const [showRetired, setShowRetired] = useState(false);

  const retire = useMutation(
    (row: MaterialRow) => api.post<MaterialRow>(`/materials/${row.id}/retire`, { row_version: row.row_version }),
    {
      invalidates: INVALIDATES,
      onSuccess: () => {
        toast.push('已停用：不再按它入库新批号，已有批号照常可用');
        setRetiring(null);
      },
    },
  );
  const restore = useMutation(
    (row: MaterialRow) => api.post<MaterialRow>(`/materials/${row.id}/restore`, { row_version: row.row_version }),
    { invalidates: INVALIDATES, onSuccess: () => toast.push('已恢复在用') },
  );

  const rows = (materials.data ?? []).filter((row) => showRetired || row.state === 'active');
  const retiredCount = (materials.data ?? []).filter((row) => row.state !== 'active').length;
  const categories = useMemo(
    () => Array.from(new Set((materials.data ?? []).map((row) => row.category).filter(Boolean))).sort(),
    [materials.data],
  );

  return (
    <Panel
      title="物料主数据"
      aside={
        <>
          {retiredCount ? (
            <label className="small">
              <input type="checkbox" checked={showRetired} onChange={(event) => setShowRetired(event.target.checked)} />{' '}
              显示已停用（{retiredCount}）
            </label>
          ) : null}
          {can('material.edit') ? (
            <button className="btn sm" onClick={() => setEditing('new')}>
              登记物料
            </button>
          ) : null}
        </>
      }
      flush
    >
      <ListState loading={materials.loading && !materials.data} error={materials.error} />
      {materials.data && !rows.length ? <Empty>还没有物料主数据：登记物料，或入库批号时按名称与单位顺带建</Empty> : null}
      {rows.length ? (
        <table>
          <thead>
            <tr>
              <th>编码</th>
              <th>名称</th>
              <th>类别</th>
              <th>基础单位</th>
              <th>单位换算</th>
              <th className="num">批号</th>
              <th>状态</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className={row.state === 'active' ? undefined : 'retired-row'}>
                <td className="mono">{row.code}</td>
                <td>
                  {row.name}
                  {row.cas || row.ghs.length ? (
                    <div className="tiny muted">
                      {[row.cas ? `CAS ${row.cas}` : '', row.ghs.join('、')].filter(Boolean).join(' · ')}
                    </div>
                  ) : null}
                </td>
                <td>{row.category || <span className="muted">未分类</span>}</td>
                <td className="mono">{row.base_unit}</td>
                <td className="small mono">
                  {Object.entries(row.conversions).map(([unit, factor]) => (
                    <div key={unit}>
                      1 {unit} = {factor} {row.base_unit}
                    </div>
                  ))}
                  {Object.keys(row.conversions).length ? null : <span className="muted">—</span>}
                </td>
                <td className="num mono">{row.lot_count}</td>
                <td>
                  <Pill state={row.state === 'active' ? 'running' : 'aborted'} label={row.state === 'active' ? '在用' : '已停用'} />
                </td>
                <td className="row-end">
                  {can('material.edit') && row.state === 'active' ? (
                    <>
                      <button className="btn sm" onClick={() => setEditing(row)}>
                        编辑
                      </button>
                      <button className="btn sm danger" onClick={() => setRetiring(row)}>
                        停用
                      </button>
                    </>
                  ) : null}
                  {can('material.edit') && row.state !== 'active' ? (
                    <button
                      className="btn sm"
                      disabled={restore.pending}
                      onClick={() => restore.run(row).catch((error) => toast.push(error.message))}
                    >
                      恢复
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <div className="panel-body small muted">
        类别决定配液模板怎么加这种料；单位换算写「1 单位折合多少基础单位」，入库与设备回报按它折成基础单位记账，没登记的跨量纲单位一律拒收。
        已有批号的物料不能改名称与基础单位。
      </div>

      {editing ? (
        <MaterialDialog
          material={editing === 'new' ? undefined : editing}
          categories={categories}
          onClose={() => setEditing(null)}
        />
      ) : null}
      {retiring ? (
        <ConfirmDialog
          title={`停用物料 · ${retiring.code}`}
          danger
          confirmLabel="停用"
          pending={retire.pending}
          error={retire.error?.message}
          onClose={() => setRetiring(null)}
          onConfirm={() => retire.run(retiring).catch(() => undefined)}
        >
          <div className="note warn">
            {retiring.name} 停用后不能再按它入库新批号，配液模板导入也不再认它；已有的 {retiring.lot_count}{' '}
            个批号照常可用、可消耗。停用不删除，随时可以恢复。
          </div>
        </ConfirmDialog>
      ) : null}
    </Panel>
  );
}

type ConversionRow = { unit: string; factor: string };

/* 登记与编辑共用一个表单。编辑时只提交改过的字段，带 row_version：别人先改了会 409，不静默覆盖。 */
function MaterialDialog({
  material,
  categories,
  onClose,
}: {
  material?: MaterialRow;
  categories: string[];
  onClose: () => void;
}) {
  const toast = useToast();
  const locked = new Set(material?.locked_fields ?? []);
  const [form, setForm] = useState({
    code: material?.code ?? '',
    name: material?.name ?? '',
    base_unit: material?.base_unit ?? 'g',
    category: material?.category ?? '',
    cas: material?.cas ?? '',
    ghs: (material?.ghs ?? []).join('、'),
    external_ref: material?.external_ref ?? '',
  });
  const [conversions, setConversions] = useState<ConversionRow[]>(
    Object.entries(material?.conversions ?? {}).map(([unit, factor]) => ({ unit, factor })),
  );
  const [error, setError] = useState('');

  const save = useMutation(
    (payload: Record<string, unknown>) =>
      material ? api.patch<MaterialRow>(`/materials/${material.id}`, payload) : api.post<MaterialRow>('/materials', payload),
    {
      invalidates: INVALIDATES,
      onSuccess: () => {
        toast.push(material ? '物料主数据已更新' : '物料已登记');
        onClose();
      },
    },
  );

  const conversionMap = Object.fromEntries(
    conversions.filter((row) => row.unit.trim()).map((row) => [row.unit.trim(), row.factor.trim()]),
  );
  const ghs = form.ghs.split(/[,，、\s]+/).map((item) => item.trim()).filter(Boolean);

  const submit = () => {
    setError('');
    const full: Record<string, unknown> = {
      name: form.name.trim(), base_unit: form.base_unit.trim(), category: form.category.trim(), cas: form.cas.trim(),
      ghs, external_ref: form.external_ref.trim(), conversions: conversionMap,
    };
    if (!material) {
      save.run({ code: form.code.trim(), ...full }).catch((caught) => setError(caught.message));
      return;
    }
    const before: Record<string, unknown> = {
      name: material.name, base_unit: material.base_unit, category: material.category, cas: material.cas,
      ghs: material.ghs, external_ref: material.external_ref, conversions: material.conversions,
    };
    const changes = Object.fromEntries(
      Object.entries(full).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(before[key])),
    );
    if (!Object.keys(changes).length) {
      onClose();
      return;
    }
    save.run({ ...changes, row_version: material.row_version }).catch((caught) => setError(caught.message));
  };

  const lockedHint = (key: string) =>
    locked.has(key) ? `已有 ${material?.lot_count} 个批号，不能改：批号、预留与消耗按名称与单位对账` : undefined;

  return (
    <Modal
      title={material ? `编辑物料 · ${material.code}` : '登记物料'}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={save.pending || !form.name.trim() || !form.base_unit.trim() || (!material && !form.code.trim())}
            onClick={submit}
          >
            {material ? '保存' : '登记'}
          </button>
        </>
      }
    >
      <div className="grid cols-3">
        <Field label="编码" hint={material ? '登记后不可更改' : '组织内唯一，如 ELY-EC'}>
          <input
            className="mono"
            value={form.code}
            disabled={!!material}
            onChange={(event) => setForm({ ...form, code: event.target.value })}
          />
        </Field>
        <Field label="名称" hint={lockedHint('name') ?? '配方表表头、批号、设备回报都按名称对上'}>
          <input value={form.name} disabled={locked.has('name')} onChange={(event) => setForm({ ...form, name: event.target.value })} />
        </Field>
        <Field label="基础单位" hint={lockedHint('base_unit') ?? '库存按它记账，如 g、mL'}>
          <input
            className="mono"
            value={form.base_unit}
            disabled={locked.has('base_unit')}
            onChange={(event) => setForm({ ...form, base_unit: event.target.value })}
          />
        </Field>
      </div>
      <div className="grid cols-3">
        <Field label="类别" hint="配液模板按类别决定怎么加这种料">
          <input
            value={form.category}
            list="material-categories"
            onChange={(event) => setForm({ ...form, category: event.target.value })}
          />
          <datalist id="material-categories">
            {categories.map((category) => (
              <option key={category} value={category} />
            ))}
          </datalist>
        </Field>
        <Field label="CAS">
          <input className="mono" value={form.cas} onChange={(event) => setForm({ ...form, cas: event.target.value })} />
        </Field>
        <Field label="外部编号" hint="ERP / LIMS 里的物料号，可空">
          <input className="mono" value={form.external_ref} onChange={(event) => setForm({ ...form, external_ref: event.target.value })} />
        </Field>
      </div>
      <Field label="GHS 危险性" hint="逗号或顿号分隔，如 易燃液体、急性毒性">
        <input value={form.ghs} onChange={(event) => setForm({ ...form, ghs: event.target.value })} />
      </Field>

      <div className="field">
        <span>单位换算</span>
        {conversions.map((row, index) => (
          <div key={index} className="row" style={{ gap: 6, alignItems: 'center' }}>
            <span className="small">1</span>
            <input
              className="mono"
              style={{ width: 90 }}
              value={row.unit}
              placeholder="mL"
              aria-label="换算单位"
              onChange={(event) =>
                setConversions(conversions.map((item, at) => (at === index ? { ...item, unit: event.target.value } : item)))
              }
            />
            <span className="small">=</span>
            <input
              className="mono"
              style={{ width: 140 }}
              value={row.factor}
              placeholder="1.32"
              aria-label="折合基础单位数"
              onChange={(event) =>
                setConversions(conversions.map((item, at) => (at === index ? { ...item, factor: event.target.value } : item)))
              }
            />
            <span className="small mono">{form.base_unit || '基础单位'}</span>
            <button className="btn sm" onClick={() => setConversions(conversions.filter((_, at) => at !== index))}>
              删除
            </button>
          </div>
        ))}
        <div>
          <button className="btn sm" onClick={() => setConversions([...conversions, { unit: '', factor: '' }])}>
            加一条换算
          </button>
        </div>
        <span className="small muted">
          写实测或供应商给的精确值，如密度 1.32 g/mL 就写「1 mL = 1.32 g」。同量纲的公制换算（mg ↔ g、μL ↔ mL）不用登记。
          改了只影响之后的入库与入账，已入账的流水不回溯。
        </span>
      </div>
      {error ? <div className="note bad">{error}</div> : null}
    </Modal>
  );
}
