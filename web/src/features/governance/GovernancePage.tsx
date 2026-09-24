import { useMemo, useState } from 'react';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { useSignature } from '../../shared/signature';
import type {
  AccountRow, AccessLogRow, RoleKey, RolePermissions, SchemaState, ServiceIdentityRow,
} from '../../shared/types';
import {
  ConfirmDialog, Field, ListState, Modal, Panel, Pill, useToast,
} from '../../shared/ui';

type SecretResult = { source: string; secret: string };
type TemporaryPassword = { id: string; username: string; temporary_password: string };

export function GovernancePage() {
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<ServiceIdentityRow | null>(null);
  const [rotating, setRotating] = useState<ServiceIdentityRow | null>(null);
  const [changingState, setChangingState] = useState<ServiceIdentityRow | null>(null);
  const [issued, setIssued] = useState<SecretResult | null>(null);
  const [creatingAccount, setCreatingAccount] = useState(false);
  const [editingAccount, setEditingAccount] = useState<AccountRow | null>(null);
  const [resettingAccount, setResettingAccount] = useState<AccountRow | null>(null);
  const [temporaryPassword, setTemporaryPassword] = useState<TemporaryPassword | null>(null);

  const identities = useQuery<ServiceIdentityRow[]>(
    'governance:identities', () => api.get('/service-identities'), 15000,
  );
  const schema = useQuery<SchemaState>(
    'governance:schema', () => api.get('/admin/schema'), 30000,
  );
  const access = useQuery<AccessLogRow[]>(
    'governance:access-log', () => api.get('/admin/access-log?limit=100'), 15000,
  );
  const accounts = useQuery<AccountRow[]>(
    'governance:accounts', () => api.get('/admin/accounts'), 15000,
  );

  const rotate = useMutation(
    (id: string) => api.post<SecretResult>(`/service-identities/${id}/rotate`),
    {
      invalidates: ['governance:identities', 'audit'],
      onSuccess: (result) => {
        setRotating(null);
        setIssued(result);
      },
    },
  );
  const state = useMutation(
    (row: ServiceIdentityRow) => api.post(`/service-identities/${row.id}/state`, {
      state: row.state === 'active' ? 'disabled' : 'active',
    }),
    {
      invalidates: ['governance:identities', 'audit'],
      onSuccess: () => {
        toast.push('服务身份状态已更新');
        setChangingState(null);
      },
    },
  );
  const resetPassword = useMutation(
    (id: string) => api.post<TemporaryPassword>(`/admin/accounts/${id}/reset-password`),
    {
      invalidates: ['governance:accounts', 'audit'],
      onSuccess: (result) => {
        setResettingAccount(null);
        setTemporaryPassword(result);
      },
    },
  );

  const rows = identities.data ?? [];
  const denied = access.data ?? [];
  const accountRows = accounts.data ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>系统治理</h1>
          <div className="small muted">管理设备与外部系统凭据、授权范围、数据库兼容状态和拒绝访问记录</div>
        </div>
      </div>

      <div className="metrics">
        <div className="metric">
          <span className="metric-label">有效账号</span>
          <strong className="metric-value">{accountRows.filter((row) => row.account_state === 'active' && row.membership_state === 'active').length}</strong>
          <span className="metric-hint">待首次改密 {accountRows.filter((row) => row.must_change_password).length}</span>
        </div>
        <div className="metric">
          <span className="metric-label">数据库结构</span>
          <strong className="metric-value" style={{ fontSize: 14 }}>
            {schema.data?.compatible ? '兼容' : schema.loading ? '检查中' : '不兼容'}
          </strong>
          <span className="metric-hint mono">{schema.data?.current ?? '—'} / {schema.data?.expected ?? '—'}</span>
        </div>
        <div className="metric">
          <span className="metric-label">有效服务身份</span>
          <strong className="metric-value">{rows.filter((row) => row.state === 'active').length}</strong>
          <span className="metric-hint">停用 {rows.filter((row) => row.state !== 'active').length}</span>
        </div>
        <div className="metric">
          <span className="metric-label">近期拒绝访问</span>
          <strong className="metric-value">{denied.length}</strong>
          <span className="metric-hint">只显示当前组织，最多 100 条</span>
        </div>
      </div>

      <Panel
        title={`账号与成员（${accountRows.length}）`}
        aside={<button className="btn primary sm" onClick={() => setCreatingAccount(true)}>创建账号</button>}
        flush
      >
        <div className="note">
          新账号和重置后的账号只显示一次临时口令，首次登录必须改密。撤销成员关系会立即阻止该账号继续访问当前组织。
        </div>
        <ListState
          loading={accounts.loading && !accounts.data}
          error={accounts.error}
          empty={!accountRows.length}
          emptyText="当前组织还没有账号。"
        />
        {accountRows.length ? (
          <table>
            <thead><tr><th>账号</th><th>角色</th><th>组织成员</th><th>口令</th><th /></tr></thead>
            <tbody>
              {accountRows.map((row) => (
                <tr key={row.id}>
                  <td><b>{row.display_name}</b><div className="tiny mono muted">{row.username}</div></td>
                  <td>{row.role_name}<div className="tiny mono muted">{row.role}</div></td>
                  <td>
                    <Pill
                      state={row.account_state === 'active' && row.membership_state === 'active' ? 'running' : 'disabled'}
                      label={row.account_state !== 'active' ? '账号停用' : row.membership_state !== 'active' ? '成员已撤销' : '有效'}
                    />
                  </td>
                  <td className="small">
                    {row.must_change_password ? <span className="warn-text">待首次改密</span> : '已修改'}
                    <div className="tiny muted">{clock(row.password_changed_at)}</div>
                  </td>
                  <td className="row-end">
                    <button className="btn sm" onClick={() => setEditingAccount(row)}>编辑</button>
                    <button className="btn sm danger" onClick={() => setResettingAccount(row)}>重置口令</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      <RolePermissionsPanel />

      <Panel
        title={`服务身份（${rows.length}）`}
        aside={<button className="btn primary sm" onClick={() => setCreating(true)}>签发凭据</button>}
        flush
      >
        <div className="note warn">
          密钥原文只显示一次。授权遵循最小范围：设备入口填工位编号，检测回传填指定任务编号；
          仅确有组织级 LIMS 集成时才选择全部检测任务。
        </div>
        <ListState
          loading={identities.loading && !identities.data}
          error={identities.error}
          empty={!rows.length}
          emptyText="还没有服务身份。接入设备、执行器或 LIMS 前请先签发独立凭据。"
        />
        {rows.length ? (
          <table>
            <thead>
              <tr><th>来源</th><th>授权范围</th><th>最近使用</th><th>状态</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td><b>{row.name || row.source}</b><div className="tiny mono muted">{row.source}</div></td>
                  <td className="small">{scopeSummary(row)}</td>
                  <td className="small mono">
                    {clock(row.last_used_at)}
                    {row.rotated_at ? <div className="tiny muted">轮换 {clock(row.rotated_at)}</div> : null}
                  </td>
                  <td><Pill state={row.state} label={row.state === 'active' ? '有效' : '已停用'} /></td>
                  <td className="row-end">
                    <button className="btn sm" onClick={() => setEditing(row)}>编辑范围</button>
                    <button className="btn sm" onClick={() => setRotating(row)}>轮换密钥</button>
                    <button
                      className={`btn sm${row.state === 'active' ? ' danger' : ''}`}
                      onClick={() => setChangingState(row)}
                    >
                      {row.state === 'active' ? '停用' : '启用'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      <Panel title={`拒绝访问记录（${denied.length}）`} flush>
        <ListState
          loading={access.loading && !access.data}
          error={access.error}
          empty={!denied.length}
          emptyText="当前组织没有拒绝访问记录。"
        />
        {denied.length ? (
          <table>
            <thead><tr><th>时间</th><th>主体</th><th>请求</th><th>错误码</th><th>原因</th><th>请求 ID</th></tr></thead>
            <tbody>
              {denied.map((row) => (
                <tr key={row.id}>
                  <td className="small mono">{clock(row.time)}</td>
                  <td>{row.subject || '—'}<div className="tiny muted">{row.subject_kind}</div></td>
                  <td className="small mono">{row.method} {row.path}</td>
                  <td><span className="tag bad">{row.code}</span></td>
                  <td className="small">{row.reason}</td>
                  <td className="tiny mono muted">{row.request_id || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      {creating ? <IdentityForm onClose={() => setCreating(false)} onIssued={setIssued} /> : null}
      {editing ? <IdentityForm identity={editing} onClose={() => setEditing(null)} /> : null}
      {rotating ? (
        <ConfirmDialog
          title={`轮换密钥 · ${rotating.source}`}
          danger
          confirmLabel="立即轮换"
          pending={rotate.pending}
          error={rotate.error?.message}
          onConfirm={() => void rotate.run(rotating.id)}
          onClose={() => setRotating(null)}
        >
          旧密钥会立即失效。请先确认设备或集成系统可以同步更新；新密钥只显示一次。
        </ConfirmDialog>
      ) : null}
      {changingState ? (
        <ConfirmDialog
          title={`${changingState.state === 'active' ? '停用' : '启用'} · ${changingState.source}`}
          danger={changingState.state === 'active'}
          confirmLabel={changingState.state === 'active' ? '确认停用' : '确认启用'}
          pending={state.pending}
          error={state.error?.message}
          onConfirm={() => void state.run(changingState)}
          onClose={() => setChangingState(null)}
        >
          {changingState.state === 'active'
            ? '停用会立即拒绝这个来源的新心跳、设备回执和结果回传，不影响已经入账的历史记录。'
            : '启用后该来源可按当前授权范围重新访问接口。'}
        </ConfirmDialog>
      ) : null}
      {issued ? <SecretModal result={issued} onClose={() => setIssued(null)} /> : null}
      {creatingAccount ? <AccountForm onClose={() => setCreatingAccount(false)} onIssued={setTemporaryPassword} /> : null}
      {editingAccount ? <AccountForm account={editingAccount} onClose={() => setEditingAccount(null)} /> : null}
      {resettingAccount ? (
        <ConfirmDialog
          title={`重置口令 · ${resettingAccount.username}`}
          danger
          confirmLabel="重置并使旧口令失效"
          pending={resetPassword.pending}
          error={resetPassword.error?.message}
          onConfirm={() => void resetPassword.run(resettingAccount.id)}
          onClose={() => setResettingAccount(null)}
        >
          系统会生成一次性显示的临时口令。旧口令立即失效，当前令牌仍会在下一次业务请求时受到账号状态与成员关系校验。
        </ConfirmDialog>
      ) : null}
      {temporaryPassword ? <PasswordModal result={temporaryPassword} onClose={() => setTemporaryPassword(null)} /> : null}
    </div>
  );
}

function scopeSummary(row: ServiceIdentityRow) {
  const stations = row.scopes.stations?.length ? `工位 ${row.scopes.stations.join('、')}` : '无工位权限';
  const tasks = row.scopes.analysis_tasks === 'all'
    ? '全部检测任务'
    : row.scopes.analysis_tasks?.length ? `检测任务 ${row.scopes.analysis_tasks.join('、')}` : '无结果回传权限';
  const serials = row.scopes.instrument_serials?.length
    ? `仪器序列号 ${row.scopes.instrument_serials.join('、')}` : '';
  return <>{stations}<div className="tiny muted">{tasks}{serials ? ` · ${serials}` : ''}</div></>;
}

function list(value: string): string[] {
  return [...new Set(value.split(/[，,\n]/).map((item) => item.trim()).filter(Boolean))];
}

function IdentityForm({
  identity,
  onClose,
  onIssued,
}: {
  identity?: ServiceIdentityRow;
  onClose: () => void;
  onIssued?: (result: SecretResult) => void;
}) {
  const toast = useToast();
  const taskScope = identity?.scopes.analysis_tasks;
  const [source, setSource] = useState(identity?.source ?? '');
  const [name, setName] = useState(identity?.name ?? '');
  const [stations, setStations] = useState((identity?.scopes.stations ?? []).join('、'));
  const [allTasks, setAllTasks] = useState(taskScope === 'all');
  const [tasks, setTasks] = useState(Array.isArray(taskScope) ? taskScope.join('、') : '');
  const [serials, setSerials] = useState((identity?.scopes.instrument_serials ?? []).join('、'));

  const scopes = () => ({
    stations: list(stations),
    analysis_tasks: allTasks ? 'all' : list(tasks),
    instrument_serials: list(serials),
  });
  const save = useMutation<[], ServiceIdentityRow | SecretResult>(
    () => identity
      ? api.patch<ServiceIdentityRow>(`/service-identities/${identity.id}`, {
          name: name.trim(), scopes: scopes(), row_version: identity.row_version,
        })
      : api.post<SecretResult>('/service-identities', {
          source: source.trim(), name: name.trim(), scopes: scopes(),
        }),
    {
      invalidates: ['governance:identities', 'audit'],
      onSuccess: (result) => {
        onClose();
        if (!identity && onIssued) onIssued(result as SecretResult);
        else toast.push('服务身份授权范围已更新');
      },
    },
  );

  return (
    <Modal
      title={identity ? `编辑授权 · ${identity.source}` : '签发服务身份'}
      onClose={onClose}
      footer={(
        <><button className="btn" onClick={onClose}>取消</button><button
          className="btn primary" disabled={save.pending || (!identity && source.trim().length < 3)}
          onClick={() => void save.run()}
        >{save.pending ? '保存中…' : identity ? '保存范围' : '签发并显示密钥'}</button></>
      )}
    >
      {!identity ? <Field label="来源标识" hint="3–64 位小写字母、数字、点、下划线或连字符；签发后不可修改">
        <input value={source} onChange={(event) => setSource(event.target.value.toLowerCase())} placeholder="lims-main" />
      </Field> : null}
      <Field label="显示名称"><input value={name} onChange={(event) => setName(event.target.value)} placeholder="主 LIMS / 1 号线执行器" /></Field>
      <Field label="授权工位" hint="逗号或换行分隔；留空表示不能提交任何设备心跳或回执">
        <textarea rows={2} value={stations} onChange={(event) => setStations(event.target.value)} placeholder="ST-01-A、ST-01-B" />
      </Field>
      <label className="row small"><input type="checkbox" checked={allTasks} onChange={(event) => setAllTasks(event.target.checked)} />允许提交当前组织全部检测任务（仅组织级 LIMS 使用）</label>
      {!allTasks ? <Field label="授权检测任务" hint="逗号或换行分隔；留空表示不能回传检测结果">
        <textarea rows={2} value={tasks} onChange={(event) => setTasks(event.target.value)} placeholder="AT-..." />
      </Field> : null}
      <Field label="授权仪器序列号" hint="可选；回传声明序列号时必须位于此清单">
        <textarea rows={2} value={serials} onChange={(event) => setSerials(event.target.value)} />
      </Field>
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}

function SecretModal({ result, onClose }: { result: SecretResult; onClose: () => void }) {
  return (
    <Modal title={`凭据已签发 · ${result.source}`} onClose={onClose} footer={<button className="btn primary" onClick={onClose}>我已安全保存</button>}>
      <div className="note warn">关闭后系统不会再次显示这段密钥。请保存到密钥管理器，不要贴入设备配置 JSON、聊天或日志。</div>
      <Field label="X-Service-Source"><input className="mono" readOnly value={result.source} onFocus={(event) => event.currentTarget.select()} /></Field>
      <Field label="X-Service-Secret" hint="点击输入框后可全选复制；内网 HTTP 环境不依赖浏览器剪贴板 API">
        <textarea className="mono secret-value" readOnly rows={3} value={result.secret} onFocus={(event) => event.currentTarget.select()} />
      </Field>
    </Modal>
  );
}

const ROLES: [RoleKey, string][] = [
  ['researcher', '研究员'], ['qa', 'QA 负责人'], ['operator', '操作员'],
  ['ehs', 'EHS 专员'], ['automation_engineer', '自动化工程师'], ['lab_manager', '实验室经理'], ['auditor', '审计员'],
  ['admin', '系统管理员'],
];

function AccountForm({
  account,
  onClose,
  onIssued,
}: {
  account?: AccountRow;
  onClose: () => void;
  onIssued?: (result: TemporaryPassword) => void;
}) {
  const toast = useToast();
  const [username, setUsername] = useState(account?.username ?? '');
  const [displayName, setDisplayName] = useState(account?.display_name ?? '');
  const [roles, setRoles] = useState<RoleKey[]>(account?.roles?.length ? account.roles : [account?.role ?? 'operator']);
  const toggleRole = (value: RoleKey) =>
    setRoles((current) => (current.includes(value) ? current.filter((r) => r !== value) : [...current, value]));
  const [accountState, setAccountState] = useState(account?.account_state ?? 'active');
  const [membershipState, setMembershipState] = useState(account?.membership_state ?? 'active');
  const save = useMutation<[], AccountRow | TemporaryPassword>(
    () => account
      ? api.patch(`/admin/accounts/${account.id}`, {
          display_name: displayName.trim(), roles,
          account_state: accountState, membership_state: membershipState,
          row_version: account.row_version,
        })
      : api.post('/admin/accounts', {
          username: username.trim(), display_name: displayName.trim(), roles,
        }),
    {
      invalidates: ['governance:accounts', 'admin:members', 'audit'],
      onSuccess: (result) => {
        onClose();
        if (!account && onIssued) onIssued(result as TemporaryPassword);
        else toast.push('账号信息已更新');
      },
    },
  );

  return (
    <Modal
      title={account ? `编辑账号 · ${account.username}` : '创建账号'}
      onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>取消</button><button
        className="btn primary"
        disabled={save.pending || !displayName.trim() || !roles.length || (!account && username.trim().length < 3)}
        onClick={() => void save.run()}
      >{save.pending ? '保存中…' : account ? '保存' : '创建并显示临时口令'}</button></>}
    >
      {!account ? <Field label="登录账号" hint="3–64 位小写账号；创建后不可修改">
        <input value={username} onChange={(event) => setUsername(event.target.value.toLowerCase())} placeholder="zhang.san" />
      </Field> : null}
      <Field label="显示姓名"><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></Field>
      <Field label="角色（可多选）" hint="权限取所选角色的并集；第一个勾选的是主角色，显示在审计与签名里">
        <div className="row wrap">
          {ROLES.map(([value, label]) => (
            <label key={value} className="check">
              <input type="checkbox" checked={roles.includes(value)} onChange={() => toggleRole(value)} />
              {label}{roles[0] === value && roles.length > 1 ? <span className="tiny muted">（主）</span> : null}
            </label>
          ))}
        </div>
      </Field>
      <div className="tiny muted">多角色不影响职责分离：同一个人仍不能批准或复核自己提交、录入的内容。</div>
      {account ? <div className="grid cols-2">
        <Field label="账号状态"><select value={accountState} onChange={(event) => setAccountState(event.target.value as 'active' | 'disabled')}>
          <option value="active">有效</option><option value="disabled">停用</option>
        </select></Field>
        <Field label="当前组织成员关系"><select value={membershipState} onChange={(event) => setMembershipState(event.target.value as 'active' | 'revoked')}>
          <option value="active">有效</option><option value="revoked">已撤销</option>
        </select></Field>
      </div> : null}
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
    </Modal>
  );
}

function PasswordModal({ result, onClose }: { result: TemporaryPassword; onClose: () => void }) {
  return (
    <Modal title={`临时口令 · ${result.username}`} onClose={onClose} footer={<button className="btn primary" onClick={onClose}>我已安全交付</button>}>
      <div className="note warn">关闭后无法再次查看。请通过受控渠道交付给本人；首次登录必须修改，系统不会在审计中记录明文。</div>
      <Field label="账号"><input className="mono" readOnly value={result.username} onFocus={(event) => event.currentTarget.select()} /></Field>
      <Field label="临时口令"><textarea className="mono secret-value" readOnly rows={3} value={result.temporary_password} onFocus={(event) => event.currentTarget.select()} /></Field>
    </Modal>
  );
}

/* 角色权限矩阵：管理员按角色勾选动作权限，保存要签名。系统管理员一列恒为全选、不可改。
   矩阵只决定「角色能不能发起这个动作」；组织范围、执行资质与「本人不能审批本人」另行校验。 */
function RolePermissionsPanel() {
  const toast = useToast();
  const { sign } = useSignature();
  const { user } = useSession();
  const data = useQuery<RolePermissions>('governance:role-permissions', () => api.get('/admin/role-permissions'));
  const [draft, setDraft] = useState<Record<string, string[]> | null>(null);
  const save = useMutation(
    (body: Record<string, unknown>) => api.put<RolePermissions>('/admin/role-permissions', body),
    {
      invalidates: ['governance:role-permissions', 'audit'],
      onSuccess: () => {
        setDraft(null);
        toast.push('角色权限已更新；各账号下一次操作即按新权限判断');
      },
    },
  );
  const body = data.data;
  const matrix = draft ?? body?.matrix ?? {};
  const changed = useMemo(() => {
    if (!draft || !body) return 0;
    return Object.keys(draft).reduce((count, role) => {
      const before = new Set(body.matrix[role] ?? []);
      const after = new Set(draft[role] ?? []);
      return count + [...after].filter((p) => !before.has(p)).length + [...before].filter((p) => !after.has(p)).length;
    }, 0);
  }, [draft, body]);

  const toggle = (role: string, permission: string) =>
    setDraft((current) => {
      const base = current ?? body?.matrix ?? {};
      const perms = new Set(base[role] ?? []);
      if (perms.has(permission)) perms.delete(permission);
      else perms.add(permission);
      return { ...base, [role]: [...perms].sort() };
    });

  const commit = async () => {
    if (!body || !draft) return;
    const target = `role-permissions:${user?.organization_id ?? ''}`;
    const signatureId = await sign('修改角色权限', target, ['权限变更批准'], body.row_version);
    if (!signatureId) return;
    await save.run({ matrix: draft, row_version: body.row_version, signature_id: signatureId });
  };

  return (
    <Panel
      title="角色与权限"
      aside={
        draft ? (
          <div className="row">
            <button className="btn sm" onClick={() => setDraft(null)}>取消</button>
            <button className="btn sm" onClick={() => body && setDraft({ ...body.defaults })}>恢复出厂默认</button>
            <button className="btn primary sm" disabled={!changed || save.pending} onClick={() => void commit()}>
              {save.pending ? '保存中…' : `签名保存（${changed} 处变更）`}
            </button>
          </div>
        ) : (
          <button className="btn sm" disabled={!body} onClick={() => body && setDraft({ ...body.matrix })}>编辑权限</button>
        )
      }
      flush
    >
      <div className="note">
        勾选决定每个角色能发起哪些动作；一个账号挂多个角色时取并集，在上方「账号与成员」里分配。
        系统管理员恒有全部权限。权限矩阵不改变职责分离（本人不能批准、复核自己提交或录入的内容）和执行资质要求。
        {body ? (
          <span className="tiny muted">
            {' '}
            {body.customized ? `当前为自定义矩阵 v${body.row_version} · ${body.updated_by} · ${clock(body.updated_at)}` : '当前为出厂默认矩阵'}
          </span>
        ) : null}
      </div>
      {user?.admin_self_approval ? (
        <div className="note warn">
          测试环境已开启 ILCS_ADMIN_SELF_APPROVAL：系统管理员可以审批、复核本人提交或录入的内容，每次都记一条「测试环境管理员自审」审计。其他角色不受影响；正式环境不能开启。
        </div>
      ) : null}
      <ListState loading={data.loading && !body} error={data.error} empty={false} emptyText="" />
      {save.error ? <div className="note bad">{save.error.message}</div> : null}
      {body ? (
        <table className="perm-matrix">
          <thead>
            <tr>
              <th>权限</th>
              {body.roles.map((role) => <th key={role.key} className="center">{role.name}</th>)}
            </tr>
          </thead>
          <tbody>
            {body.catalog.map((group) => (
              <PermissionGroup
                key={group.group}
                group={group}
                roles={body.roles}
                matrix={matrix}
                defaults={body.defaults}
                editing={!!draft}
                onToggle={toggle}
              />
            ))}
          </tbody>
        </table>
      ) : null}
    </Panel>
  );
}

function PermissionGroup({
  group,
  roles,
  matrix,
  defaults,
  editing,
  onToggle,
}: {
  group: RolePermissions['catalog'][number];
  roles: RolePermissions['roles'];
  matrix: Record<string, string[]>;
  defaults: Record<string, string[]>;
  editing: boolean;
  onToggle: (role: string, permission: string) => void;
}) {
  return (
    <>
      <tr className="group-row">
        <td colSpan={roles.length + 1}><b>{group.group}</b></td>
      </tr>
      {group.permissions.map((permission) => (
        <tr key={permission.key}>
          <td>
            {permission.label}
            <div className="tiny mono muted">{permission.key}</div>
          </td>
          {roles.map((role) => {
            const granted = role.locked || (matrix[role.key] ?? []).includes(permission.key);
            const isDefault = role.locked || (defaults[role.key] ?? []).includes(permission.key) === granted;
            return (
              <td key={role.key} className={`center${isDefault ? '' : ' changed'}`}>
                <input
                  type="checkbox"
                  aria-label={`${role.name} · ${permission.label}`}
                  checked={granted}
                  disabled={!editing || role.locked || permission.key === 'org.admin' || permission.key === 'service.manage'}
                  onChange={() => onToggle(role.key, permission.key)}
                />
              </td>
            );
          })}
        </tr>
      ))}
    </>
  );
}
