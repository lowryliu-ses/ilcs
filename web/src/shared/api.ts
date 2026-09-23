/* HTTP 边界。业务组件不直接碰 fetch，只调用这里的方法。 */

const TOKEN_KEY = 'ilcs.token';
const ORG_KEY = 'ilcs.org';
const TIMEZONE_KEY = 'ilcs.timezone';

export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(status: number, payload: unknown) {
    super(messageOf(payload) || `请求失败（${status}）`);
    this.status = status;
    this.payload = payload;
  }

  /** 稳定错误码。界面按它分支，不靠匹配中文文案。 */
  get code(): string {
    return (this.payload as { detail?: { code?: string } })?.detail?.code ?? '';
  }

  /** 服务端把阻塞项放在 detail 里，界面用它解释为什么按钮被拒绝。 */
  get blocked(): { key: string; label: string; detail?: string }[] {
    const detail = (this.payload as { detail?: { blocked?: [] } })?.detail;
    return (detail as { blocked?: [] })?.blocked ?? [];
  }

  /** 版本冲突时服务端给出当前版本，界面据此提示刷新。 */
  get currentVersion(): number | undefined {
    return (this.payload as { detail?: { current_version?: number } })?.detail?.current_version;
  }
}

function messageOf(payload: unknown): string {
  const detail = (payload as { detail?: unknown })?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: string }).message;
    if (message) return message;
    const reasons = (detail as { reasons?: string[] }).reasons;
    if (reasons?.length) return reasons.join('；');
    const blocked = (detail as { blocked?: { label: string }[] }).blocked;
    if (blocked?.length) return blocked.map((b) => b.label).join('；');
    const checks = (detail as { checks?: { label: string; detail: string }[] }).checks;
    if (checks?.length) return checks.map((c) => `${c.label}：${c.detail}`).join('；');
  }
  return '';
}

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (value: string) => localStorage.setItem(TOKEN_KEY, value),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

export const organization = {
  get: () => localStorage.getItem(ORG_KEY) ?? '',
  set: (value: string) => localStorage.setItem(ORG_KEY, value),
  clear: () => localStorage.removeItem(ORG_KEY),
};

/** 当前组织的展示时区。服务端时间统一按 UTC 传输，界面只在显示层转换。 */
export const displayTimezone = {
  get: () => localStorage.getItem(TIMEZONE_KEY) || 'Asia/Shanghai',
  set: (value: string) => localStorage.setItem(TIMEZONE_KEY, value || 'Asia/Shanghai'),
  clear: () => localStorage.removeItem(TIMEZONE_KEY),
};

/** 幂等键（UUID v4）。

   不用 `crypto.randomUUID`：它只在安全上下文里存在，内网用 http://10.10.106.51:8090 打开时
   是 undefined，调用直接抛 TypeError——新建批次就是这么崩的。
   也不写成「有就用、没有才降级」的分支：localhost 算安全上下文，那样本地永远走原生分支、
   线上永远走降级分支，出问题的那条路在开发期一次都跑不到。这里只保留一种实现。

   `crypto.getRandomValues` 没有安全上下文限制，随机性同样是密码学强度的。这一点不能将就：
   键撞了会被服务端当成重放，第二个批次会拿到第一个的响应，而不是真的创建出来。 */
export function idempotencyKey(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // 版本 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // 变体 10
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0'));
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10, 16).join(''),
  ].join('-');
}

/** 同一个逻辑操作的幂等键缓存。

   服务端的规则是「同键同内容回放、同键不同内容 409」，所以键必须跟「这一次操作」绑定，
   而不是跟「这一次 HTTP 请求」绑定。之前每次请求都新生成一个键：网络超时后重试会拿到新键，
   服务端认不出是同一次操作，于是真的执行第二遍。

   键按 (方法, 路径, 请求体) 缓存；成功后删除，失败保留——所以重试复用原键，
   而改了内容再提交自然是一次新操作、拿到新键。 */
const pendingKeys = new Map<string, string>();

function keyFor(signature: string): string {
  const existing = pendingKeys.get(signature);
  if (existing) return existing;
  const fresh = idempotencyKey();
  pendingKeys.set(signature, fresh);
  return fresh;
}

type Options = { method?: string; body?: unknown; idempotent?: boolean };

async function request<T>(path: string, options: Options = {}): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const current = token.get();
  if (current) headers.Authorization = `Bearer ${current}`;
  const org = organization.get();
  if (org) headers['X-Organization-Id'] = org;

  const method = options.method ?? 'GET';
  const signature = `${method} ${path} ${JSON.stringify(options.body ?? null)}`;
  if (options.idempotent) headers['Idempotency-Key'] = keyFor(signature);

  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  if (response.status === 401) {
    token.clear();
    window.dispatchEvent(new Event('ilcs:unauthorized'));
  }
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    // 4xx 是服务端已经做出的裁决，重试同一个键没有意义；5xx 与网络错误保留键以便重试
    if (options.idempotent && response.status < 500) pendingKeys.delete(signature);
    throw new ApiError(response.status, payload);
  }
  if (options.idempotent) pendingKeys.delete(signature);
  return payload as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown, idempotent = false) =>
    request<T>(path, { method: 'POST', body: body ?? {}, idempotent }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body: body ?? {} }),
  remove: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

  /** 文件上传。multipart 不走 JSON 分支，也不加 Content-Type——浏览器要自己带 boundary。 */
  upload: async <T>(path: string, file: File, fields: Record<string, string> = {}): Promise<T> => {
    const form = new FormData();
    form.append('file', file);
    Object.entries(fields).forEach(([key, value]) => form.append(key, value));
    const headers: Record<string, string> = {};
    const current = token.get();
    if (current) headers.Authorization = `Bearer ${current}`;
    const org = organization.get();
    if (org) headers['X-Organization-Id'] = org;
    const response = await fetch(`/api${path}`, { method: 'POST', headers, body: form });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;
    if (!response.ok) throw new ApiError(response.status, payload);
    return payload as T;
  },

  /** CSV、PDF 等非 JSON 响应走这里，交给浏览器下载。

     下载一律带令牌：服务端对每次下载做对象访问校验并写审计，直接开一个新窗口会漏掉请求头。 */
  download: async (path: string, filename: string) => {
    const headers: Record<string, string> = { Authorization: `Bearer ${token.get()}` };
    const org = organization.get();
    if (org) headers['X-Organization-Id'] = org;
    const response = await fetch(`/api${path}`, { headers });
    if (!response.ok) {
      const text = await response.text();
      throw new ApiError(response.status, text ? JSON.parse(text) : null);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = Object.assign(document.createElement('a'), { href: url, download: filename });
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};

/** 分页查询串。默认每页 20 条、最大 100 条，与服务端一致。 */
export function pageQuery(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === '' || value === false) return;
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : '';
}
