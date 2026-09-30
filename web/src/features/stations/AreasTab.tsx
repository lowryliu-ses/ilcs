/* 实验区：工位按所在区域分组（数据里是工位的岛号），这里只给编号起名字。

   工位登记时填编号；看板的「实验区负载」、现场监控的分组都按这里的名称显示，没起名的显示「实验区 #N」。
   改名不影响排程与执行（它们只认编号），留审计。可以先给还没有工位的编号起名，登记工位时直接填编号。 */
import { useState } from 'react';

import { api } from '../../shared/api';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { IslandRow } from '../../shared/types';
import { Field, ListState, Modal, NumberInput, Panel, useToast } from '../../shared/ui';

export function areaLabel(id: number, name?: string): string {
  if (!id) return '未分区';
  return name ? `${name}（实验区 #${id}）` : `实验区 #${id}`;
}

export function AreasTab() {
  const { can } = useSession();
  const areas = useQuery<IslandRow[]>('islands', () => api.get<IslandRow[]>('/islands'));
  const [editing, setEditing] = useState<{ id: number | ''; name: string; fresh: boolean } | null>(null);
  const editable = can('station.edit');
  const rows = areas.data ?? [];

  return (
    <>
      <Panel
        title={`实验区（${rows.length}）`}
        aside={
          editable ? (
            <button className="btn primary sm" onClick={() => setEditing({ id: '', name: '', fresh: true })}>登记实验区</button>
          ) : null
        }
        flush
      >
        <ListState
          loading={areas.loading && !areas.data}
          error={areas.error}
          empty={!rows.length}
          emptyText="还没有实验区：登记工位时填的编号会出现在这里，起了名字以后看板与现场监控按名称分组"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th className="num">编号</th>
                <th>名称</th>
                <th className="num">在用工位</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="num mono">#{row.id}</td>
                  <td>{row.name || <span className="muted">未起名（显示为「实验区 #{row.id}」）</span>}</td>
                  <td className="num">{row.stations}</td>
                  <td className="row-end">
                    {editable ? (
                      <button className="btn sm" onClick={() => setEditing({ id: row.id, name: row.name, fresh: false })}>
                        {row.name ? '改名' : '起名'}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>
      {editing ? <AreaDialog initial={editing} taken={rows.filter((row) => row.name).map((row) => row.id)} onClose={() => setEditing(null)} /> : null}
    </>
  );
}

function AreaDialog({
  initial, taken, onClose,
}: {
  initial: { id: number | ''; name: string; fresh: boolean };
  taken: number[];
  onClose: () => void;
}) {
  const toast = useToast();
  const [id, setId] = useState<number | ''>(initial.id);
  const [name, setName] = useState(initial.name);
  const save = useMutation(
    () => api.put<IslandRow>(`/islands/${id}`, { name: name.trim() }),
    {
      invalidates: ['islands', 'dashboard', 'floor', 'stations', 'audit'],
      onSuccess: (row) => {
        toast.push(`实验区 #${row.id} 已命名为「${row.name}」`);
        onClose();
      },
    },
  );
  const validId = typeof id === 'number' && Number.isInteger(id) && id >= 1 && id <= 9999;
  const clash = initial.fresh && validId && taken.includes(id as number);

  return (
    <Modal
      title={initial.fresh ? '登记实验区' : `实验区 #${initial.id}`}
      onClose={() => {
        if (!save.pending) onClose();
      }}
      footer={
        <>
          <button className="btn" disabled={save.pending} onClick={onClose}>取消</button>
          <button className="btn primary" disabled={save.pending || !validId || !name.trim()} onClick={() => save.run().catch(() => undefined)}>
            {save.pending ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <div className="note">名称只是给人看的：工位、排程与执行都按编号，改名不影响在跑的批次。</div>
      {initial.fresh ? (
        <Field label="编号" hint="1–9999；登记工位时在「实验区」里填这个编号">
          <NumberInput value={id} ariaLabel="实验区编号" onChange={(value) => setId(value === '' ? '' : Number(value))} />
        </Field>
      ) : null}
      <Field label="名称" hint="例如：物料准备段、配液段、测试段">
        <input value={name} maxLength={64} onChange={(event) => setName(event.target.value)} />
      </Field>
      {clash ? <div className="note warn">编号 #{id} 已经有名字，保存会把它改成新名称。</div> : null}
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}
