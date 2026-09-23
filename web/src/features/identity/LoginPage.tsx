import { useState } from 'react';

import { useSession } from '../../shared/session';
import { Field } from '../../shared/ui';

const DEMO_ACCOUNTS = [
  ['operator', '操作员：接单、批次排程下发、人工记录、异常恢复'],
  ['researcher', '研究员：实验方案、方法配方、任务分配、报告起草'],
  ['qa', 'QA 负责人：方案与报告审批、流程审核、数据复核'],
  ['ehs', 'EHS 专员：报警处置与危废'],
  ['admin', '系统管理员：组织成员、能力与工位、服务身份'],
];

export function LoginPage() {
  const { login } = useSession();
  // 演示账号只在 Vite 开发服务器显示；生产构建不能预填共享账号与口令。
  const demo = import.meta.env.DEV;
  const [username, setUsername] = useState(demo ? 'operator' : '');
  const [password, setPassword] = useState(demo ? 'ilcs1234' : '');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError('');
    try {
      await login(username, password);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '登录失败');
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <h1>ILCS 实验室平台</h1>
        <p className="small muted">
          从实验方案到报告发布的闭环。角色决定可执行的写操作，关键动作需要电子签名。
        </p>
        <Field label="账号">
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
          />
        </Field>
        <Field label="口令">
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </Field>
        {error ? <div className="note bad">{error}</div> : null}
        <button className="btn primary" type="submit" disabled={pending}>
          {pending ? '登录中…' : '登录'}
        </button>
        {demo ? <div className="login-hint">
          {DEMO_ACCOUNTS.map(([account, description]) => (
            <button type="button" key={account} className="link" onClick={() => setUsername(account)}>
              <span className="mono">{account}</span>
              <span className="muted small">{description}</span>
            </button>
          ))}
        </div> : null}
      </form>
    </div>
  );
}
