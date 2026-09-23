/* 极简服务端状态层：缓存 + 失效 + 轮询。
   接口刻意与 TanStack Query 对齐，将来换库只替换这个文件。

   实时推送（shared/stream.ts）连上时，失效由服务端变更通知驱动，轮询退到兜底频率；
   推送断开时自动回到页面声明的轮询间隔。页面不需要知道当前是哪种模式。 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { ApiError } from './api';

type Entry = { data: unknown; at: number };
type Match = (key: string) => boolean;

const cache = new Map<string, Entry>();
const listeners = new Set<(match: Match) => void>();

function dispatch(match: Match): void {
  for (const key of [...cache.keys()]) {
    if (match(key)) cache.delete(key);
  }
  listeners.forEach((notify) => notify(match));
}

export function invalidate(prefix = ''): void {
  dispatch((key) => key.startsWith(prefix));
}

/* 推送来的失效先攒一小会儿再发：一次提交常常同时改批次、步骤、指令，逐条发会让同一页面连取几次。 */
const COALESCE_MS = 300;
let pending: Match[] = [];
let flushTimer: number | undefined;

export function invalidateSoon(match: Match): void {
  pending.push(match);
  if (flushTimer !== undefined) return;
  flushTimer = window.setTimeout(() => {
    const batch = pending;
    pending = [];
    flushTimer = undefined;
    dispatch((key) => batch.some((test) => test(key)));
  }, COALESCE_MS);
}

/** 注销时只清空缓存，不通知仍在卸载过程中的页面重新请求。 */
export function clearQueries(): void {
  cache.clear();
}

/* 推送连接状态。连上时轮询只作兜底（丢通知、跨进程漏发），间隔拉长到至少 60 s。 */
export type LiveState = { live: boolean; since: number };
let liveState: LiveState = { live: false, since: Date.now() };
const liveListeners = new Set<() => void>();
const FALLBACK_POLL_MS = 60000;

export function setLive(live: boolean): void {
  if (liveState.live === live) return;
  liveState = { live, since: Date.now() };
  liveListeners.forEach((notify) => notify());
}

export function useLive(): LiveState {
  return useSyncExternalStore(
    (notify) => {
      liveListeners.add(notify);
      return () => liveListeners.delete(notify);
    },
    () => liveState,
  );
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

  const { live } = useLive();
  const interval = pollMs ? (live ? Math.max(pollMs, FALLBACK_POLL_MS) : pollMs) : 0;

  useEffect(() => {
    if (!key) return;
    const cached = cache.get(key);
    if (cached) setData(cached.data as T);
    void load();
    const notify = (match: Match) => {
      if (match(key)) void load();
    };
    listeners.add(notify);
    // 后台标签页不轮询：中控室常开着一排标签，每个都按秒级频率打接口没有意义
    const timer = interval
      ? window.setInterval(() => {
          if (!document.hidden) void load();
        }, interval)
      : undefined;
    return () => {
      listeners.delete(notify);
      if (timer) window.clearInterval(timer);
    };
  }, [key, load, interval]);

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
