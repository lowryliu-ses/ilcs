import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import { api, displayTimezone, token } from './api';
import { clearQueries, invalidate } from './query';
import type { User } from './types';

type SessionValue = {
  user: User | null;
  ready: boolean;
  can: (permission: string) => boolean;
  login: (username: string, password: string) => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  logout: () => void;
};

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  const logout = useCallback(() => {
    // invalidate() 会同步通知当前页面重新请求；若先删令牌，就会在注销瞬间制造一批
    // 无意义的 401。注销只需清缓存，登录后的 invalidate() 再加载新会话数据。
    clearQueries();
    token.clear();
    displayTimezone.clear();
    setUser(null);
  }, []);

  useEffect(() => {
    const onUnauthorized = () => logout();
    window.addEventListener('ilcs:unauthorized', onUnauthorized);
    return () => window.removeEventListener('ilcs:unauthorized', onUnauthorized);
  }, [logout]);

  useEffect(() => {
    if (!token.get()) {
      setReady(true);
      return;
    }
    api
      .get<User>('/auth/me')
      .then((profile) => {
        displayTimezone.set(profile.organization_timezone);
        setUser(profile);
      })
      .catch(() => token.clear())
      .finally(() => setReady(true));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const result = await api.post<{ access_token: string; user: User }>('/auth/login', { username, password });
    token.set(result.access_token);
    displayTimezone.set(result.user.organization_timezone);
    setUser(result.user);
    invalidate();
  }, []);

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    const profile = await api.post<User>('/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword,
    });
    displayTimezone.set(profile.organization_timezone);
    setUser(profile);
    invalidate();
  }, []);

  const value = useMemo<SessionValue>(
    () => ({
      user,
      ready,
      can: (permission) => !!user?.perms.includes(permission),
      login,
      changePassword,
      logout,
    }),
    [user, ready, login, changePassword, logout],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error('useSession 必须在 SessionProvider 内使用');
  return value;
}
