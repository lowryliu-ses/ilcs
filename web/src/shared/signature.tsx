/* 电子签名。签名 ticket 由服务端签发，一次性、两分钟有效，业务写接口凭它落审计。 */
import { createContext, useCallback, useContext, useState } from 'react';
import type { ReactNode } from 'react';

import { api } from './api';
import { useSession } from './session';
import { Field, Modal } from './ui';

type Request = {
  action: string;
  target: string;
  meanings: string[];
  objectVersion: number;
  resolve: (signatureId: string | null) => void;
};

type SignatureValue = {
  /** 打开签名对话框；用户取消时返回 null。

     `objectVersion` 是被操作对象的当前版本：票据会绑定动作 + 对象 ID + 对象版本，
     所以为一个对象签发的签名用不到另一个对象上，对象在签名后被别人改过也会被拒。 */
  sign: (
    action: string,
    target: string,
    meanings: string[],
    objectVersion?: number,
  ) => Promise<string | null>;
};

const SignatureContext = createContext<SignatureValue>({ sign: async () => null });

export function SignatureProvider({ children }: { children: ReactNode }) {
  const { user } = useSession();
  const [request, setRequest] = useState<Request | null>(null);
  const [password, setPassword] = useState('');
  const [meaning, setMeaning] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);

  const sign = useCallback<SignatureValue['sign']>((action, target, meanings, objectVersion = 0) => {
    setPassword('');
    setNote('');
    setError('');
    setMeaning(meanings[0] ?? '');
    return new Promise((resolve) =>
      setRequest({ action, target, meanings, objectVersion, resolve }),
    );
  }, []);

  const close = (signatureId: string | null) => {
    request?.resolve(signatureId);
    setRequest(null);
  };

  const submit = async () => {
    if (!request) return;
    if (!password) {
      setError('请输入口令后再签名');
      return;
    }
    setPending(true);
    try {
      const result = await api.post<{ signature_id: string }>('/signatures', {
        password,
        meaning,
        action: request.action,
        target: request.target,
        note,
        object_version: request.objectVersion,
      });
      close(result.signature_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '签名失败');
    } finally {
      setPending(false);
    }
  };

  return (
    <SignatureContext.Provider value={{ sign }}>
      {children}
      {request ? (
        <Modal
          title="电子签名确认"
          onClose={() => close(null)}
          footer={
            <>
              <button className="btn" onClick={() => close(null)}>
                取消
              </button>
              <button className="btn primary" onClick={submit} disabled={pending}>
                {pending ? '签名中…' : '签名并执行'}
              </button>
            </>
          }
        >
          <div className="note">
            操作 <b>{request.action}</b>，对象 <span className="tag mono">{request.target}</span>
            {request.objectVersion ? <> · 版本 <span className="tag mono">v{request.objectVersion}</span></> : null}。
            签名一次性使用，并绑定这个动作、这个对象和这个版本；将写入不可修改的审计记录。
          </div>
          <div className="grid cols-2">
            <Field label="签名人">
              <input value={user?.display_name ?? ''} readOnly />
            </Field>
            <Field
              label="口令"
              hint={`当前登录账号 ${user?.username ?? ''} 的登录口令，所有签名场景都是同一个`}
            >
              <input
                type="password"
                autoFocus
                value={password}
                autoComplete="current-password"
                onChange={(event) => setPassword(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && submit()}
              />
            </Field>
          </div>
          <Field label="签名含义" hint="你以什么名义签这一笔；选项随动作不同，会原样进审计记录">
            <select value={meaning} onChange={(event) => setMeaning(event.target.value)}>
              {request.meanings.map((option) => (
                <option key={option}>{option}</option>
              ))}
            </select>
          </Field>
          <Field label="备注（可选）">
            <textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
          </Field>
          {error ? <div className="note bad">{error}</div> : null}
        </Modal>
      ) : null}
    </SignatureContext.Provider>
  );
}

export function useSignature(): SignatureValue {
  return useContext(SignatureContext);
}
