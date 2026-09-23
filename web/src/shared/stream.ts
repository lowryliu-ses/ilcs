/* 服务端变更推送（/api/stream，Server-Sent Events 格式）。

   推送只说「什么变了」，不带数据：收到后把对应查询标记失效，页面带着自己的令牌重取。
   所以推送丢一条最多是晚刷新，不会显示越权或错误的数据；断线期间的变更由重连后的整体重取补上。

   不用浏览器原生 EventSource：它不能带 Authorization 头，而令牌放进 URL 会进访问日志。
   这里用 fetch 读流，自己解析 SSE 帧。 */
import { organization, token } from './api';
import { invalidate, invalidateSoon, setLive } from './query';

type Change = { topic: string; ids: string[] };

/* 主题 → 需要重取的查询。只列跟着变的页面数据，其余页面靠兜底轮询。 */
function matcherFor(change: Change): (key: string) => boolean {
  const ids = change.ids ?? [];
  const one = (prefix: string) => (key: string) => key === prefix || key.startsWith(`${prefix}:`);
  const batchKeys = (key: string) =>
    ids.length ? ids.some((id) => key === `batches:${id}` || key.startsWith(`recovery:${id}`)) : one('batches')(key);
  switch (change.topic) {
    case 'batches':
      return (key) => key === 'batches' || batchKeys(key) || one('dashboard')(key) || key === 'schedule:queue' || one('tasks')(key);
    case 'commands':
      return (key) => key === 'commands' || key === 'batches' || batchKeys(key) || one('dashboard')(key) || one('floor')(key);
    case 'alarms':
      return (key) => key === 'alarms' || one('dashboard')(key) || (key.startsWith('batches:') && !key.endsWith(':telemetry'));
    case 'stations':
      return (key) => key === 'stations' || key.startsWith('stations:') || one('dashboard')(key) || one('floor')(key);
    case 'gate':
      return (key) => key === 'gate' || key === 'schedule:queue';
    case 'schedule':
      return (key) => one('schedule')(key) || one('dashboard')(key) || one('floor')(key);
    case 'telemetry':
      return (key) => (ids.length ? ids.some((id) => key === `batches:${id}:telemetry`) : key.endsWith(':telemetry'));
    case 'labware':
      return (key) => one('labware')(key) || one('floor')(key) || one('locations')(key) || (key.startsWith('batches:') && !key.endsWith(':telemetry'));
    default:
      return () => false;
  }
}

function parseFrame(frame: string): { event: string; data: string } | null {
  let event = 'message';
  const data: string[] = [];
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const at = line.indexOf(':');
    const field = at < 0 ? line : line.slice(0, at);
    const value = at < 0 ? '' : line.slice(at + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  return data.length || event !== 'message' ? { event, data: data.join('\n') } : null;
}

/** 建立推送连接并保持重连；返回停止函数。登录后启动，注销时停止。 */
export function startStream(): () => void {
  let stopped = false;
  let controller: AbortController | undefined;
  let retryMs = 1000;
  let timer: number | undefined;
  let lastByteAt = Date.now();
  // 服务端每 15 s 发一次保活帧。代理或网络静默断开时连接不会报错，只是再也没有字节：
  // 超过这个时长没收到任何东西就主动断开重连，并回到轮询，不让界面停在一条死连接上
  const SILENCE_MS = 40000;
  const watchdog = window.setInterval(() => {
    if (controller && Date.now() - lastByteAt > SILENCE_MS) controller.abort();
  }, 5000);

  const onVisible = () => {
    // 从后台切回来：后台期间不轮询，整体重取一次
    if (!document.hidden) invalidateSoon(() => true);
  };
  document.addEventListener('visibilitychange', onVisible);

  const schedule = (ms: number) => {
    if (stopped) return;
    timer = window.setTimeout(() => void connect(), ms);
  };

  const connect = async () => {
    const current = token.get();
    if (stopped || !current) return;
    controller = new AbortController();
    lastByteAt = Date.now();
    const headers: Record<string, string> = { Authorization: `Bearer ${current}`, Accept: 'text/event-stream' };
    const org = organization.get();
    if (org) headers['X-Organization-Id'] = org;
    let reachedHello = false;
    try {
      const response = await fetch('/api/stream', { headers, signal: controller.signal, cache: 'no-store' });
      if (response.status === 401 || response.status === 403) {
        setLive(false);
        return; // 令牌失效：会话层会带用户回登录页，不在这里反复重试
      }
      if (!response.ok || !response.body) throw new Error(`stream ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        lastByteAt = Date.now();
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
        let split = buffer.indexOf('\n\n');
        while (split >= 0) {
          const frame = parseFrame(buffer.slice(0, split));
          buffer = buffer.slice(split + 2);
          split = buffer.indexOf('\n\n');
          if (!frame) continue;
          if (frame.event === 'hello') {
            reachedHello = true;
            retryMs = 1000;
            setLive(true);
            // 断线期间可能漏了通知：连上后整体重取一次
            invalidateSoon(() => true);
          } else if (frame.event === 'resync') {
            invalidate('');
          } else if (frame.event === 'change') {
            try {
              invalidateSoon(matcherFor(JSON.parse(frame.data) as Change));
            } catch {
              /* 坏帧忽略：最多晚一次刷新 */
            }
          }
        }
      }
      // 服务端按最长存活时间正常结束：立刻重连（顺便重新鉴权）
      schedule(reachedHello ? 200 : retryMs);
    } catch {
      if (stopped) return;
      setLive(false);
      schedule(retryMs);
      retryMs = Math.min(30000, retryMs * 2);
    }
  };

  void connect();
  return () => {
    stopped = true;
    window.clearInterval(watchdog);
    document.removeEventListener('visibilitychange', onVisible);
    if (timer !== undefined) window.clearTimeout(timer);
    controller?.abort();
    setLive(false);
  };
}
