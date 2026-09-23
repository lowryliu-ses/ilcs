/* 极简服务端状态层：缓存 + 失效 + 轮询。
   接口刻意与 TanStack Query 对齐，将来换库只替换这个文件。 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from './api';

type Entry = { data: unknown; at: number };

const cache = new Map<string, Entry>();
const listeners = new Set<(prefix: string) => void>();

export function invalidate(prefix = ''): void {
  for (const key of [...cache.keys()]) {
    if (key.startsWith(prefix)) cache.delete(key);
  }
  listeners.forEach((notify) => notify(prefix));
}

/** 注销时只清空缓存，不通知仍在卸载过程中的页面重新请求。 */
export function clearQueries(): void {
  cache.clear();
}

export type QueryState<T> = {
  data: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  refresh: () => void;
};

export function useQuery<T>(key: string | null, fetcher: () => Promise<T>, pollMs = 0): QueryState<T> {
  const [data, setData] = useState<T | undefined>(() => (key ? (cache.get(key)?.data as T) : undefined));
  const [error, setError] = useState<ApiError | undefined>();
  const [loading, setLoading] = useState(!!key && !cache.has(key ?? ''));
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(async () => {
    if (!key) return;
    setLoading(true);
    try {
      const result = await fetcherRef.current();
      cache.set(key, { data: result, at: Date.now() });
      setData(result);
      setError(undefined);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : new ApiError(0, { detail: String(caught) }));
    } finally {
      setLoading(false);
    }
  }, [key]);

  useEffect(() => {
    if (!key) return;
    const cached = cache.get(key);
    if (cached) setData(cached.data as T);
    void load();
    const notify = (prefix: string) => {
      if (key.startsWith(prefix)) void load();
    };
    listeners.add(notify);
    const timer = pollMs ? window.setInterval(() => void load(), pollMs) : undefined;
    return () => {
      listeners.delete(notify);
      if (timer) window.clearInterval(timer);
    };
  }, [key, load, pollMs]);

  return { data, error, loading, refresh: load };
}

export function useMutation<TArgs extends unknown[], TResult>(
  action: (...args: TArgs) => Promise<TResult>,
  options: { invalidates?: string[]; onSuccess?: (result: TResult) => void } = {},
) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | undefined>();

  const run = useCallback(
    async (...args: TArgs) => {
      setPending(true);
      setError(undefined);
      try {
        const result = await action(...args);
        (options.invalidates ?? ['']).forEach(invalidate);
        options.onSuccess?.(result);
        return result;
      } catch (caught) {
        const normalized = caught instanceof ApiError ? caught : new ApiError(0, { detail: String(caught) });
        setError(normalized);
        throw normalized;
      } finally {
        setPending(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [action],
  );

  return { run, pending, error, clearError: () => setError(undefined) };
}
