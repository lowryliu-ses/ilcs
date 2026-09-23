import { FormEvent, useState } from 'react';

import { useSession } from '../../shared/session';
import { Field } from '../../shared/ui';

export function ChangePasswordPage() {
  const { user, changePassword, logout } = useSession();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (next !== confirm) {
      setError('两次输入的新口令不一致');
      return;
    }
    setPending(true);
    setError('');
    try {
      await changePassword(current, next);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '修改口令失败');
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <h1>首次登录必须修改口令</h1>
        <p className="small muted">
          账号 <b className="mono">{user?.username}</b> 使用的是管理员签发的临时口令。
          修改成功前不能进入业务页面。
        </p>
        <Field label="当前临时口令"><input type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} autoFocus /></Field>
        <Field label="新口令" hint="至少 12 位，并包含大写字母、小写字母、数字中的至少两类">
          <input type="password" autoComplete="new-password" value={next} onChange={(event) => setNext(event.target.value)} />
        </Field>
        <Field label="再次输入新口令"><input type="password" autoComplete="new-password" value={confirm} onChange={(event) => setConfirm(event.target.value)} /></Field>
        {error ? <div className="note bad">{error}</div> : null}
        <button className="btn primary" type="submit" disabled={pending || !current || !next || !confirm}>
          {pending ? '修改中…' : '修改口令并进入系统'}
        </button>
        <button className="btn" type="button" onClick={logout}>退出并换账号</button>
      </form>
    </div>
  );
}
