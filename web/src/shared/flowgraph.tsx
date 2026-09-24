/* 流程图：方法编辑器与批次运行视图共用。

   布局按依赖图分层：每个节点的列 = 从起点出发的最长路径长度，同一列里按前驱所在行的重心排，
   这样分叉的几条路上下铺开、汇合处落在它们中间。不做自由摆放——节点位置由依赖关系决定，
   画面和执行顺序永远对得上，也不用把坐标存进方法里。

   连线画在真实的前驱与后继之间（不是列表里相邻的两个卡片）；条件分支的出边带出口名，
   回环画成从分支底部绕回目标的虚线。编辑模式下每个节点右侧有一个连接柄：按住拖到另一个
   节点上就建一条依赖，点连线可以删掉它。

   不引图形库：节点是普通 DOM（可以放表单控件与按钮），连线是一层内联 SVG，颜色走 CSS 令牌。 */
import type { ReactNode } from 'react';
import { useMemo, useRef, useState } from 'react';

/** 节点面板拖拽的数据类型。面板项用 `event.dataTransfer.setData(PALETTE_TYPE, payload)`。 */
export const PALETTE_TYPE = 'application/x-ilcs-palette';

export type FlowGraphNode = {
  id: string;
  /** 列表下标：同层排序的兜底，也就是拓扑序 */
  index: number;
  content: ReactNode;
  /** 运行视图里的步骤状态（completed / running / failed / not_taken ...） */
  state?: string;
  className?: string;
  title?: string;
  /** 所属子流程（运行视图里把展开出来的步骤框在一起） */
  group?: { id: string; label: string };
};

export type FlowGraphEdge = {
  from: string;
  to: string;
  label?: string;
  /** branch：分支出边；dead：没走到的路；taken：已经走过；hard：带硬时限 */
  tone?: 'branch' | 'dead' | 'taken' | 'hard';
  title?: string;
};

export type FlowGraphLoop = { from: string; to: string; label: string };

const NODE_W = 208;
const NODE_H = 100;
const GAP_X = 72;
const GAP_Y = 26;
const PAD = 28;
const LOOP_SPACE = 56;

type Point = { x: number; y: number };

function layout(nodes: FlowGraphNode[], edges: FlowGraphEdge[]) {
  const position = new Map(nodes.map((node, order) => [node.id, order]));
  const preds: number[][] = nodes.map(() => []);
  edges.forEach((edge) => {
    const from = position.get(edge.from);
    const to = position.get(edge.to);
    if (from !== undefined && to !== undefined && from !== to) preds[to].push(from);
  });
  // 节点按拓扑序给出（列表就是拓扑序），一遍就能算出最长路径层号
  const depth: number[] = [];
  nodes.forEach((_, order) => {
    depth[order] = preds[order].length ? Math.max(...preds[order].map((parent) => (depth[parent] ?? 0) + 1)) : 0;
  });
  const columns: number[][] = [];
  nodes.forEach((_, order) => (columns[depth[order]] ??= []).push(order));
  const row: number[] = [];
  columns.forEach((members, column) => {
    const keyed = members.map((order) => {
      const parents = preds[order].filter((parent) => row[parent] !== undefined);
      const center = column && parents.length ? parents.reduce((sum, parent) => sum + row[parent], 0) / parents.length : order;
      return { order, center };
    });
    keyed.sort((a, b) => a.center - b.center || nodes[a.order].index - nodes[b.order].index);
    let next = 0;
    keyed.forEach(({ order, center }) => {
      // 尽量对齐前驱所在的行，但同一列里不叠在一起
      const wanted = column ? Math.max(next, Math.round(center)) : next;
      row[order] = wanted;
      next = wanted + 1;
    });
  });
  const at = new Map<string, Point>();
  nodes.forEach((node, order) => {
    at.set(node.id, { x: PAD + depth[order] * (NODE_W + GAP_X), y: PAD + row[order] * (NODE_H + GAP_Y) });
  });
  const columnsCount = Math.max(1, columns.length);
  const rows = Math.max(1, ...row.map((value) => value + 1));
  return {
    at,
    width: PAD * 2 + columnsCount * NODE_W + (columnsCount - 1) * GAP_X,
    height: PAD * 2 + rows * NODE_H + (rows - 1) * GAP_Y,
  };
}

function edgePath(from: Point, to: Point) {
  const start = { x: from.x + NODE_W, y: from.y + NODE_H / 2 };
  const end = { x: to.x, y: to.y + NODE_H / 2 };
  const bend = Math.max(36, (end.x - start.x) / 2);
  const c1 = { x: start.x + bend, y: start.y };
  const c2 = { x: end.x - bend, y: end.y };
  const mid = {
    x: (start.x + 3 * c1.x + 3 * c2.x + end.x) / 8,
    y: (start.y + 3 * c1.y + 3 * c2.y + end.y) / 8,
  };
  return { d: `M ${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`, mid };
}

export function FlowGraph({
  nodes,
  edges,
  loops = [],
  selected,
  editable = false,
  onSelect,
  onConnect,
  onRemoveEdge,
  onDropPalette,
  empty,
}: {
  nodes: FlowGraphNode[];
  edges: FlowGraphEdge[];
  loops?: FlowGraphLoop[];
  selected?: string | null;
  editable?: boolean;
  onSelect?: (id: string) => void;
  onConnect?: (from: string, to: string) => void;
  onRemoveEdge?: (from: string, to: string) => void;
  /** 从节点面板拖进来的东西；落在某个节点上时给出那个节点 */
  onDropPalette?: (payload: string, onto: string | null) => void;
  empty?: ReactNode;
}) {
  const stage = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ from: string; pointer: Point } | null>(null);
  const [edgeSel, setEdgeSel] = useState<string | null>(null);
  const [dropOver, setDropOver] = useState(false);
  const { at, width, height } = useMemo(() => layout(nodes, edges), [nodes, edges]);
  const stageHeight = height + (loops.length ? LOOP_SPACE : 0);

  const local = (clientX: number, clientY: number): Point => {
    const rect = stage.current?.getBoundingClientRect();
    return { x: clientX - (rect?.left ?? 0), y: clientY - (rect?.top ?? 0) };
  };
  const nodeAt = (point: Point): string | null => {
    for (const node of nodes) {
      const origin = at.get(node.id);
      if (origin && point.x >= origin.x && point.x <= origin.x + NODE_W && point.y >= origin.y && point.y <= origin.y + NODE_H) {
        return node.id;
      }
    }
    return null;
  };

  const groups = useMemo(() => {
    const boxes = new Map<string, { label: string; x1: number; y1: number; x2: number; y2: number }>();
    nodes.forEach((node) => {
      const origin = at.get(node.id);
      if (!node.group || !origin) return;
      const box = boxes.get(node.group.id) ?? { label: node.group.label, x1: Infinity, y1: Infinity, x2: -Infinity, y2: -Infinity };
      box.x1 = Math.min(box.x1, origin.x);
      box.y1 = Math.min(box.y1, origin.y);
      box.x2 = Math.max(box.x2, origin.x + NODE_W);
      box.y2 = Math.max(box.y2, origin.y + NODE_H);
      boxes.set(node.group.id, box);
    });
    return [...boxes.entries()];
  }, [nodes, at]);

  if (!nodes.length) {
    return (
      <div
        className={`fgraph${dropOver ? ' over' : ''}`}
        onDragOver={(event) => {
          if (!editable || !onDropPalette) return;
          event.preventDefault();
          setDropOver(true);
        }}
        onDragLeave={() => setDropOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDropOver(false);
          const payload = event.dataTransfer.getData(PALETTE_TYPE);
          if (payload) onDropPalette?.(payload, null);
        }}
      >
        <div className="empty">{empty}</div>
      </div>
    );
  }

  const bottom = height - PAD + 18;

  return (
    <div className={`fgraph${dropOver ? ' over' : ''}`}>
      <div
        ref={stage}
        className="fgraph-stage"
        style={{ width, height: stageHeight }}
        onPointerMove={(event) => {
          if (drag) setDrag({ ...drag, pointer: local(event.clientX, event.clientY) });
        }}
        onPointerUp={(event) => {
          if (!drag) return;
          const target = nodeAt(local(event.clientX, event.clientY));
          if (target && target !== drag.from) onConnect?.(drag.from, target);
          setDrag(null);
        }}
        onPointerLeave={() => setDrag(null)}
        onClick={(event) => {
          if (event.target === stage.current) setEdgeSel(null);
        }}
        onDragOver={(event) => {
          if (!editable || !onDropPalette || !event.dataTransfer.types.includes(PALETTE_TYPE)) return;
          event.preventDefault();
          setDropOver(true);
        }}
        onDragLeave={() => setDropOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDropOver(false);
          const payload = event.dataTransfer.getData(PALETTE_TYPE);
          if (payload) onDropPalette?.(payload, nodeAt(local(event.clientX, event.clientY)));
        }}
      >
        <svg className="fgraph-svg" width={width} height={stageHeight}>
          <defs>
            {(['base', 'branch', 'dead', 'taken', 'hard', 'loop'] as const).map((tone) => (
              <marker key={tone} id={`fg-arrow-${tone}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" className={`fg-arrow ${tone}`} />
              </marker>
            ))}
          </defs>
          {groups.map(([id, box]) => (
            <g key={id}>
              <rect className="fg-group" x={box.x1 - 10} y={box.y1 - 22} width={box.x2 - box.x1 + 20} height={box.y2 - box.y1 + 32} rx={10} />
              <text className="fg-group-label" x={box.x1 - 2} y={box.y1 - 8}>
                子流程 · {box.label}
              </text>
            </g>
          ))}
          {edges.map((edge) => {
            const from = at.get(edge.from);
            const to = at.get(edge.to);
            if (!from || !to) return null;
            const { d } = edgePath(from, to);
            const key = `${edge.from}->${edge.to}`;
            const tone = edge.tone ?? 'base';
            return (
              <g key={key}>
                <path d={d} className={`fg-edge ${tone}${edgeSel === key ? ' sel' : ''}`} markerEnd={`url(#fg-arrow-${tone})`} />
                {editable ? (
                  <path
                    d={d}
                    className="fg-edge-hit"
                    onClick={(event) => {
                      event.stopPropagation();
                      setEdgeSel(edgeSel === key ? null : key);
                    }}
                  >
                    <title>{edge.title ?? '点击选中这条依赖'}</title>
                  </path>
                ) : null}
              </g>
            );
          })}
          {loops.map((loop, order) => {
            const from = at.get(loop.from);
            const to = at.get(loop.to);
            if (!from || !to) return null;
            const depthY = bottom + 14 + order * 10;
            const start = { x: from.x + NODE_W / 2, y: from.y + NODE_H };
            const end = { x: to.x + NODE_W / 2, y: to.y + NODE_H };
            return (
              <path
                key={`${loop.from}~${loop.to}~${order}`}
                d={`M ${start.x} ${start.y} C ${start.x} ${depthY}, ${end.x} ${depthY}, ${end.x} ${end.y}`}
                className="fg-edge loop"
                markerEnd="url(#fg-arrow-loop)"
              />
            );
          })}
          {drag ? (() => {
            const from = at.get(drag.from);
            if (!from) return null;
            return (
              <path
                d={`M ${from.x + NODE_W} ${from.y + NODE_H / 2} L ${drag.pointer.x} ${drag.pointer.y}`}
                className="fg-edge dragging"
                markerEnd="url(#fg-arrow-base)"
              />
            );
          })() : null}
        </svg>

        {edges.map((edge) => {
          const from = at.get(edge.from);
          const to = at.get(edge.to);
          if (!from || !to) return null;
          const key = `${edge.from}->${edge.to}`;
          const { mid } = edgePath(from, to);
          const chosen = edgeSel === key;
          if (!edge.label && !chosen) return null;
          return (
            <div key={`label-${key}`} className={`fg-label ${edge.tone ?? 'base'}`} style={{ left: mid.x, top: mid.y }}>
              {edge.label ? <span>{edge.label}</span> : null}
              {chosen && editable ? (
                <button
                  type="button"
                  className="fg-remove"
                  title="删除这条依赖"
                  onClick={(event) => {
                    event.stopPropagation();
                    setEdgeSel(null);
                    onRemoveEdge?.(edge.from, edge.to);
                  }}
                >
                  删除连线
                </button>
              ) : null}
            </div>
          );
        })}
        {loops.map((loop, order) => {
          const from = at.get(loop.from);
          const to = at.get(loop.to);
          if (!from || !to) return null;
          return (
            <div
              key={`loop-label-${order}`}
              className="fg-label loop"
              style={{ left: (from.x + to.x + NODE_W) / 2, top: bottom + 8 + order * 10 }}
            >
              <span>↺ {loop.label}</span>
            </div>
          );
        })}

        {nodes.map((node) => {
          const origin = at.get(node.id);
          if (!origin) return null;
          const classes = ['fg-node', node.className ?? '', node.state ? `st-${node.state}` : '', selected === node.id ? 'sel' : '']
            .filter(Boolean)
            .join(' ');
          return (
            <div
              key={node.id}
              className={classes}
              style={{ left: origin.x, top: origin.y, width: NODE_W, height: NODE_H }}
              title={node.title}
              role="button"
              tabIndex={0}
              onClick={() => onSelect?.(node.id)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onSelect?.(node.id);
                }
              }}
            >
              {node.content}
              {editable && onConnect ? (
                <span
                  className="fg-handle"
                  title="按住拖到另一个节点上：建立依赖"
                  onPointerDown={(event) => {
                    event.stopPropagation();
                    event.preventDefault();
                    setDrag({ from: node.id, pointer: local(event.clientX, event.clientY) });
                  }}
                />
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
