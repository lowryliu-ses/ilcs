/* 样本与载具标签：二维码 + 编号。二维码内容就是条码（样本没有条码时是样本号），
   扫码后走同一个查找入口（样本详情、载具放置、交接都认它）。 */
import { useQuery } from './query';
import { api } from './api';

export type QrPayload = { id: string; content: string; svg: string; label: string[] };

export function qrSrc(svg: string): string {
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

/** 打开一个只含标签的窗口并调用打印。标签 50 × 30 mm，适配常见热敏标签纸。 */
export function printLabel(payload: QrPayload): void {
  const win = window.open('', '_blank', 'width=420,height=320');
  if (!win) return;
  const lines = payload.label.filter(Boolean).map((line) => `<div>${escapeHtml(line)}</div>`).join('');
  win.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(payload.content)}</title>
<style>@page{size:50mm 30mm;margin:2mm}body{margin:0;font:10px system-ui,sans-serif;display:flex;gap:6px;align-items:center}
img{width:24mm;height:24mm}b{font:bold 11px ui-monospace,monospace;display:block;margin-bottom:2px}</style></head>
<body><img src="${qrSrc(payload.svg)}" alt=""><div><b>${escapeHtml(payload.content)}</b>${lines}</div>
<script>window.onload=function(){window.print();}</script></body></html>`);
  win.document.close();
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char] ?? char);
}

export function QrLabel({ path, cacheKey }: { path: string; cacheKey: string }) {
  const qr = useQuery<QrPayload>(cacheKey, () => api.get<QrPayload>(path));
  if (!qr.data) return <div className="small muted">{qr.error ? qr.error.message : '生成二维码…'}</div>;
  return (
    <div className="row" style={{ alignItems: 'center', gap: 12 }}>
      <img src={qrSrc(qr.data.svg)} alt={`二维码 ${qr.data.content}`} style={{ width: 96, height: 96, background: '#fff' }} />
      <div>
        <div className="mono">{qr.data.content}</div>
        <div className="tiny muted">{qr.data.label.slice(1).filter(Boolean).join(' · ')}</div>
        <button className="btn sm" onClick={() => printLabel(qr.data!)}>
          打印标签
        </button>
      </div>
    </div>
  );
}
