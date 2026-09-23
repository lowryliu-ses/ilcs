import { useState } from 'react';

import { api, pageQuery } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { CapabilityRow, MemberRow, Paged, PersonRow, QualificationRow } from '../../shared/types';
import {
  ConfirmDialog, Empty, Field, ListState, Modal, Pager, Panel, Pill, useToast,
} from '../../shared/ui';

const EMPLOYMENT: [string, string][] = [
  ['on_duty', '在岗'],
  ['leave', '休假'],
  ['left', '离岗'],
];

export function PeoplePage() {
  const { can } = useSession();
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [state, setState] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<PersonRow | null>(null);

  const query = pageQuery({ page, page_size: 20, keyword, state });
  const people = useQuery<Paged<PersonRow>>(
    `people:${query}`, () => api.get<Paged<PersonRow>>(`/people${query}`),
  );
  const expiring = useQuery<QualificationRow[]>(
    'people:expiring', () => api.get<QualificationRow[]>('/people/expiring-qualifications'),
  );

  const rows = people.data?.items ?? [];
  const soon = expiring.data ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>人员与资质</h1>
        <span className="small muted">
          资质在分配任务时按预计执行时间校验，实际开始与恢复时再校验一次。到期、撤销、停用账号都会阻止新的受控操作。
        </span>
      </div>

      {soon.length ? (
        <div className="banner warn">
          {soon.filter((row) => row.status === 'expired').length} 项资质已过期、
          {soon.filter((row) => row.status === 'expiring').length} 项即将到期：
          {soon.slice(0, 4).map((row) => `${row.person_name} ${row.label}`).join('；')}
        </div>
      ) : null}

      <Panel
        title={`人员（${people.data?.total ?? 0}）`}
        aside={
          <div className="filters">
            <input
              placeholder="姓名或编号"
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
              <option value="">全部在岗状态</option>
              {EMPLOYMENT.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            {can('person.edit') ? (
              <button className="btn primary sm" onClick={() => setCreating(true)}>
                建立档案
              </button>
            ) : null}
          </div>
        }
        flush
      >
        <ListState
          loading={people.loading && !people.data}
          error={people.error}
          empty={!rows.length}
          emptyText="没有符合条件的人员"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>编号</th>
                <th>姓名</th>
                <th>账号</th>
                <th>在岗</th>
                <th>资质</th>
                <th>可执行系统任务</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((person) => (
                <tr key={person.id}>
                  <td className="mono">{person.code}</td>
                  <td>
                    <b>{person.name}</b>
                    <div className="tiny muted">{person.title || '—'} · {person.contact || '—'}</div>
                  </td>
                  <td className="small mono">
                    {person.username || '未绑定'}
                    {person.username && !person.account_active ? (
                      <div className="tiny bad-text">账号已停用</div>
                    ) : null}
                  </td>
                  <td>
                    <Pill state={person.employment_state} label={person.employment_label} />
                  </td>
                  <td className="small">
                    有效 {person.valid_qualification_count}/{person.qualification_count}
                    {person.expiring_count ? (
                      <div className="tiny warn-text">{person.expiring_count} 项即将到期</div>
                    ) : null}
                  </td>
                  <td className="small">
                    {person.employable ? (
                      <span className="tag">可分配</span>
                    ) : (
                      <span className="tag bad">
                        {!person.username ? '未绑定有效账号' : '不在岗或账号停用'}
                      </span>
                    )}
                  </td>
                  <td className="row-end">
                    {can('person.edit') ? (
                      <button className="btn sm" onClick={() => setEditing(person)}>
                        编辑
                      </button>
                    ) : null}
                    <button className="btn sm" onClick={() => setSelected(person.id)}>
                      资质
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <Pager
          page={people.data?.page ?? 1}
          pageSize={people.data?.page_size ?? 20}
          total={people.data?.total ?? 0}
          onChange={setPage}
        />
      </Panel>

      {selected ? <PersonDialog personId={selected} onClose={() => setSelected(null)} /> : null}
      {creating ? <CreateDialog onClose={() => setCreating(false)} /> : null}
      {editing ? <EditDialog person={editing} onClose={() => setEditing(null)} /> : null}
    </div>
  );
}

function PersonDialog({ personId, onClose }: { personId: string; onClose: () => void }) {
  const { can } = useSession();
  const toast = useToast();
  const detail = useQuery<PersonRow>(`people:${personId}`, () => api.get<PersonRow>(`/people/${personId}`));
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const [granting, setGranting] = useState(false);
  const [revoking, setRevoking] = useState<QualificationRow | null>(null);

  const person = detail.data;
  const rows = person?.qualifications ?? [];

  const revoke = useMutation(
    (payload: { id: string; reason: string }) =>
      api.post(`/people/qualifications/${payload.id}/revoke`, { reason: payload.reason }),
    {
      invalidates: ['people', 'dashboard'],
      onSuccess: () => {
        toast.push('资质已撤销；运行中设备不自动急停，已产生告警并阻止下一受控操作');
        setRevoking(null);
      },
    },
  );

  return (
    <Modal title={`资质 · ${person?.name ?? personId}`} onClose={onClose} wide>
      {person ? (
        <>
          <div className="note">
            {person.employable
              ? '该人员在岗且账号有效，可以被分配系统任务。'
              : '该人员当前不能执行系统任务：需要在岗且绑定有效账号。'}
          </div>

          <div className="panel-aside" style={{ justifyContent: 'flex-end', margin: '10px 0' }}>
            {can('qualification.edit') ? (
              <button className="btn primary sm" onClick={() => setGranting(true)}>
                登记资质
              </button>
            ) : null}
          </div>

          {rows.length ? (
            <table>
              <thead>
                <tr>
                  <th>资质</th>
                  <th>生效</th>
                  <th>到期</th>
                  <th>状态</th>
                  <th>证据</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <b>{row.label}</b>
                      <div className="tiny muted mono">{row.scope_kind}:{row.scope_ref}</div>
                    </td>
                    <td className="small">{clock(row.effective_from)}</td>
                    <td className="small">{row.expires_at ? clock(row.expires_at) : '未设'}</td>
                    <td>
                      <Pill state={row.status} label={STATUS_LABEL[row.status] ?? row.status} />
                      {row.revoke_reason ? <div className="tiny muted">{row.revoke_reason}</div> : null}
                    </td>
                    <td className="small">
                      {row.evidence_file_id ? (
                        <button
                          className="btn sm"
                          onClick={() =>
                            api
                              .download(`/files/${row.evidence_file_id}/download`, `${row.label}.pdf`)
                              .catch((error) => toast.push(error.message))
                          }
                        >
                          查看
                        </button>
                      ) : (
                        <span className="muted">未上传</span>
                      )}
                    </td>
                    <td className="row-end">
                      {row.revoked_at || !can('qualification.edit') ? null : (
                        <button className="btn sm danger" onClick={() => setRevoking(row)}>
                          撤销
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>该人员还没有资质记录</Empty>
          )}
        </>
      ) : (
        <ListState loading={detail.loading} error={detail.error} />
      )}

      {granting ? (
        <GrantDialog
          personId={personId}
          capabilities={capabilities.data ?? []}
          onClose={() => setGranting(false)}
        />
      ) : null}
      {revoking ? (
        <ConfirmDialog
          title={`撤销资质 · ${revoking.label}`}
          danger
          confirmLabel="撤销"
          reasonLabel="撤销理由"
          reasonPlaceholder="例如：年度复审未通过"
          pending={revoke.pending}
          error={revoke.error?.message}
          onConfirm={(reason) => revoke.run({ id: revoking.id, reason }).catch(() => undefined)}
          onClose={() => setRevoking(null)}
        >
          <div className="note warn">
            撤销后该人员不能再发起相关受控操作。已在运行的设备不会自动急停——系统产生告警并阻止下一个受控操作，
            现场动作按设备与已批准的恢复规则处理。
          </div>
        </ConfirmDialog>
      ) : null}
    </Modal>
  );
}

const STATUS_LABEL: Record<string, string> = {
  valid: '有效', expiring: '即将到期', expired: '已过期', revoked: '已撤销', pending: '未生效',
};

function GrantDialog({
  personId,
  capabilities,
  onClose,
}: {
  personId: string;
  capabilities: CapabilityRow[];
  onClose: () => void;
}) {
  const toast = useToast();
  const [kind, setKind] = useState<'capability' | 'sop' | 'safety'>('capability');
  const [ref, setRef] = useState('');
  const [label, setLabel] = useState('');
  const [expires, setExpires] = useState('');
  const [file, setFile] = useState<{ id: string; filename: string } | null>(null);

  const upload = useMutation(
    (picked: File) => api.upload<{ id: string; filename: string }>('/files', picked, { ref_type: 'qualification' }),
    { onSuccess: (result) => setFile(result) },
  );
  const grant = useMutation(
    () =>
      api.post(`/people/${personId}/qualifications`, {
        scope_kind: kind,
        scope_ref: ref,
        label,
        evidence_file_id: file?.id ?? '',
        expires_at: expires ? new Date(expires).toISOString() : null,
      }),
    {
      invalidates: ['people', 'dashboard'],
      onSuccess: () => {
        toast.push('资质已登记');
        onClose();
      },
    },
  );

  return (
    <Modal
      title="登记资质"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!ref || grant.pending}
            onClick={() => grant.run().catch(() => undefined)}
          >
            登记
          </button>
        </>
      }
    >
      <Field label="资质类型">
        <select value={kind} onChange={(event) => { setKind(event.target.value as typeof kind); setRef(''); }}>
          <option value="capability">设备操作</option>
          <option value="sop">SOP 版本</option>
          <option value="safety">安全操作</option>
        </select>
      </Field>
      {kind === 'capability' ? (
        <Field label="能力">
          <select value={ref} onChange={(event) => setRef(event.target.value)}>
            <option value="">选择能力</option>
            {capabilities.filter((row) => !row.retired).map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
        </Field>
      ) : (
        <Field label={kind === 'sop' ? 'SOP 版本 ID' : '安全类别'}>
          <input value={ref} onChange={(event) => setRef(event.target.value)} />
        </Field>
      )}
      <Field label="显示名称" hint="留空则按类型自动生成">
        <input value={label} onChange={(event) => setLabel(event.target.value)} />
      </Field>
      <Field label="到期时间" hint="不填表示长期有效；到期后系统自动阻止相关操作">
        <input type="datetime-local" value={expires} onChange={(event) => setExpires(event.target.value)} />
      </Field>
      <Field label="证据文件">
        <input
          type="file"
          accept="application/pdf,image/png,image/jpeg"
          onChange={(event) => {
            const picked = event.target.files?.[0];
            if (picked) upload.run(picked).catch((error) => toast.push(error.message));
            event.target.value = '';
          }}
        />
        {file ? <span className="small">已上传 {file.filename}</span> : null}
      </Field>
      {upload.error ? <div className="note bad">{upload.error.message}</div> : null}
      {grant.error ? <div className="note bad">{grant.error.message}</div> : null}
    </Modal>
  );
}

/* 账号选择器。原来这里是手填 UUID——没人记得住账号的 UUID，填错了还会把资质
   挂到别人头上。已被其他档案绑定的账号列出但不可选：一个账号只该对应一份档案。 */
function AccountPicker({
  value,
  personId,
  onChange,
}: {
  value: string;
  personId?: string;
  onChange: (userId: string) => void;
}) {
  const members = useQuery<MemberRow[]>('admin:members', () => api.get<MemberRow[]>('/admin/members'));

  if (members.error) {
    return <div className="note bad">账号清单读取失败：{members.error.message}</div>;
  }
  return (
    <select value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">暂不绑定</option>
      {(members.data ?? []).map((row) => {
        const takenByOther = row.bound_person_id && row.bound_person_id !== personId;
        return (
          <option key={row.user_id} value={row.user_id} disabled={Boolean(takenByOther)}>
            {row.username} · {row.display_name}
            {row.active ? '' : '（账号已停用）'}
            {takenByOther ? `（已绑定 ${row.bound_person}）` : ''}
          </option>
        );
      })}
    </select>
  );
}

/* 改档案。在岗状态是判据不是标签：离岗或休假的人不能被分配新任务，
   已在执行的批次按既定策略处置，不由这里代劳。 */
function EditDialog({ person, onClose }: { person: PersonRow; onClose: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({
    name: person.name,
    title: person.title,
    contact: person.contact,
    lab_id: person.lab_id,
    employment_state: person.employment_state,
    user_id: person.user_id,
    note: person.note,
  });

  const save = useMutation(
    () => api.patch(`/people/${person.id}`, { ...form, row_version: person.row_version }),
    {
      invalidates: ['people', 'dashboard', 'audit'],
      onSuccess: () => {
        toast.push('人员档案已更新');
        onClose();
      },
    },
  );

  return (
    <Modal
      title={`编辑档案 · ${person.code}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!form.name || save.pending}
            onClick={() => save.run().catch(() => undefined)}
          >
            保存
          </button>
        </>
      }
    >
      <Field label="姓名">
        <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
      </Field>
      <div className="grid cols-2">
        <Field label="岗位">
          <input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} />
        </Field>
        <Field label="联系方式">
          <input value={form.contact} onChange={(event) => setForm({ ...form, contact: event.target.value })} />
        </Field>
      </div>
      <Field
        label="在岗状态"
        hint={
          form.employment_state === 'on_duty'
            ? '可被分配受控任务'
            : '休假与离岗都不能被分配新任务；已在执行的批次需要人工处置'
        }
      >
        <select
          value={form.employment_state}
          onChange={(event) => setForm({ ...form, employment_state: event.target.value })}
        >
          <option value="on_duty">在岗</option>
          <option value="leave">休假</option>
          <option value="left">离岗</option>
        </select>
      </Field>
      <Field label="关联账号" hint="只有绑定了有效账号的人员才能执行系统任务">
        <AccountPicker
          value={form.user_id}
          personId={person.id}
          onChange={(userId) => setForm({ ...form, user_id: userId })}
        />
      </Field>
      <Field label="备注">
        <textarea
          rows={2}
          value={form.note}
          onChange={(event) => setForm({ ...form, note: event.target.value })}
        />
      </Field>
      {person.qualification_count ? (
        <div className="note">
          这份档案有 {person.qualification_count} 项资质记录。改档案不动资质；
          资质的授予与撤销走「资质」里的记录，各自留痕。
        </div>
      ) : null}
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}

function CreateDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState({ code: '', name: '', title: '', contact: '', user_id: '' });
  const create = useMutation(() => api.post('/people', form), {
    invalidates: ['people'],
    onSuccess: () => {
      toast.push('人员档案已建立');
      onClose();
    },
  });

  return (
    <Modal
      title="建立人员档案"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn primary"
            disabled={!form.code || !form.name || create.pending}
            onClick={() => create.run().catch(() => undefined)}
          >
            建立
          </button>
        </>
      }
    >
      <div className="note">
        人员档案与登录账号是两件事。只有绑定了有效账号的人员才能执行系统任务；未绑定的档案可以先建，用于资质留档。
      </div>
      <Field label="人员编号">
        <input value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} />
      </Field>
      <Field label="姓名">
        <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
      </Field>
      <Field label="岗位">
        <input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} />
      </Field>
      <Field label="联系方式">
        <input value={form.contact} onChange={(event) => setForm({ ...form, contact: event.target.value })} />
      </Field>
      <Field label="关联账号" hint="留空表示暂不绑定，可用于资质留档">
        <AccountPicker value={form.user_id} onChange={(userId) => setForm({ ...form, user_id: userId })} />
      </Field>
      {create.error ? <div className="note bad">{create.error.message}</div> : null}
    </Modal>
  );
}
