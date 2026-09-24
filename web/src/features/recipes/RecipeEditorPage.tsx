/* 方法图形化编辑：节点面板 → 流程图 → 属性面板。

   设计约束：
   1. 步骤绑定能力而不是设备，参数实时对照全部工位极限的并集；
   2. 流程图按依赖关系分层排布，连线画在真实的前驱与后继之间；从节点右侧的连接柄拖到另一个
      节点上就建一条依赖，点连线可以删掉。列表顺序始终保持拓扑序（后端要求前驱排在前面），
      每次改依赖都会自动重排；
   3. 条件分支的出边带出口名，回环画成虚线；子流程节点引用一个已发布的方法，时长按它的关键路径算；
   4. 恢复规则由能力继承，配方不可覆盖，所以属性面板里只读展示。

   这里的校验是即时提示，能不能提交由服务端重算（`domain/recipe_rules.py`）。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api } from '../../shared/api';
import { FlowGraph, PALETTE_TYPE, type FlowGraphEdge, type FlowGraphLoop, type FlowGraphNode } from '../../shared/flowgraph';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type {
  BomItem, BranchCase, CapabilityRow, DeviceMethodRow, FormField, LotRow, RecipeDetail, RecipeStep, RecipeSummary,
  SopVersionRow, StationRow,
} from '../../shared/types';
import { CheckList, Field, NumberInput, Panel, Pill, useToast } from '../../shared/ui';
import {
  AUTOMATIC_KINDS,
  SKIPPABLE_KINDS,
  STEP_KINDS,
  TIMEOUT_ACTIONS,
  TIMEOUT_ACTIONS_BY_KIND,
  ancestors,
  branchCases,
  canSubmit,
  defaultParams,
  descendants,
  durationOf,
  editorChecks,
  explicitAfter,
  forwardCaseKeys,
  freeCase,
  graphMode,
  indexCapabilities,
  kindOf,
  needsStation,
  nextStepId,
  paramRange,
  predecessors,
  stationsForStep,
  stepIdOf,
  stepIssues,
  topoSort,
  whenOf,
  wouldCycle,
  type StepKind,
  type SubflowIndex,
} from './rules';

type Meta = {
  name: string;
  plate: number | '';
  design: string;
  risk: string;
  /** 关联的受控 SOP 版本。空字符串表示不关联。 */
  sop_version_id: string;
};
type Draft = { steps: RecipeStep[]; bom: BomItem[]; meta: Meta };

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/* 校验清单里显示的是人看得懂的 SOP 名，不是版本 UUID。 */
function sopLabel(versions: SopVersionRow[] | undefined, versionId: string): string {
  if (!versionId) return '';
  const hit = (versions ?? []).find((row) => row.id === versionId);
  return hit ? `${hit.code} ${hit.title} ${hit.version}` : versionId;
}

/** 新节点的默认内容。不同类型适用的字段不同，这里只填各自必需的那些。 */
function blankStep(kind: Exclude<StepKind, 'device'>): RecipeStep {
  switch (kind) {
    case 'manual':
      return {
        kind, name: '人工步骤', cap: '', params: {}, dur: 15, requires_sample_check: true,
        form: [{ key: 'value', label: '记录值', type: 'number', required: true }],
      };
    case 'wait':
      return { kind, name: '等待', cap: '', params: {}, dur: 30, wait_for: { mode: 'duration' } };
    case 'gate':
      return { kind, name: '质检关卡', cap: '', params: {}, dur: 0, gate: { scope: 'batch', on_fail: 'hold', max_rework: 2 } };
    case 'split':
      return { kind, name: '样本拆分', cap: '', params: {}, dur: 0, split: { count: 4, child_type: '' } };
    case 'branch':
      return {
        kind, name: '条件分支', cap: '', params: {}, dur: 0,
        branch: {
          mode: 'manual',
          cases: [{ key: 'yes', label: '是' }, { key: 'no', label: '否' }],
        },
      };
    case 'subflow':
      return { kind, name: '子流程', cap: '', params: {}, dur: 0, subflow: { recipe_id: '' } };
    case 'notify':
      return { kind, name: '消息通知', cap: '', params: {}, dur: 0, notify: { message: '' } };
    default:
      return { kind: 'review', name: '审核', cap: '', params: {}, dur: 0, review_role: 'qa' };
  }
}

const NON_DEVICE: [Exclude<StepKind, 'device'>, string][] = [
  ['manual', '人工'],
  ['wait', '等待'],
  ['review', '审核'],
  ['gate', '质检关卡'],
  ['split', '样本拆分'],
  ['branch', '条件分支'],
  ['subflow', '子流程'],
  ['notify', '消息通知'],
];

export function RecipeEditorPage() {
  const { recipeId = '' } = useParams();
  const navigate = useNavigate();
  const { can } = useSession();
  const toast = useToast();

  const recipe = useQuery<RecipeDetail>(`recipes:${recipeId}`, () => api.get<RecipeDetail>(`/recipes/${recipeId}`));
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const stations = useQuery<StationRow[]>('stations', () => api.get<StationRow[]>('/stations'));
  const lots = useQuery<LotRow[]>('lots', () => api.get<LotRow[]>('/lots'));
  const methods = useQuery<DeviceMethodRow[]>('device-methods:released', () =>
    api.get<DeviceMethodRow[]>('/device-methods?state=released'),
  );
  const recipes = useQuery<RecipeSummary[]>('recipes', () => api.get<RecipeSummary[]>('/recipes'));
  // 只取已发布且生效的版本：草稿与已退役的 SOP 不该被新方法引用
  const sops = useQuery<SopVersionRow[]>('sops:effective', () => api.get<SopVersionRow[]>('/sops/effective'));

  const [draft, setDraft] = useState<Draft | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const loadedFor = useRef('');

  /* 载入编辑缓冲。已有未保存改动时不被轮询回填冲掉。 */
  useEffect(() => {
    const data = recipe.data;
    if (!data || loadedFor.current === data.id) return;
    loadedFor.current = data.id;
    setDraft({
      steps: clone(data.steps ?? []),
      bom: clone(data.bom ?? []),
      meta: {
        name: data.name, plate: data.plate, design: data.design, risk: data.risk,
        sop_version_id: data.sop_version_id ?? '',
      },
    });
    setSelected(data.steps?.length ? stepIdOf(data.steps[0], 0) : null);
    setDirty(false);
  }, [recipe.data]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  const invalidates = [`recipes:${recipeId}`, 'recipes', 'dashboard', 'audit'];
  const save = useMutation((payload: Record<string, unknown>) => api.patch<RecipeDetail>(`/recipes/${recipeId}`, payload), {
    invalidates,
  });
  const submit = useMutation(() => api.post(`/recipes/${recipeId}/submit`), { invalidates });

  const capabilityIndex = useMemo(() => indexCapabilities(capabilities.data), [capabilities.data]);
  const subflowIndex = useMemo<SubflowIndex>(
    () =>
      Object.fromEntries(
        (recipes.data ?? []).map((row) => [
          row.id,
          { name: row.name, version: row.version, state: row.state, needs_revision: row.needs_revision, critical_path_min: row.critical_path_min ?? 0 },
        ]),
      ),
    [recipes.data],
  );
  const materials = useMemo(
    () => [...new Set((lots.data ?? []).map((lot) => lot.material))].sort(),
    [lots.data],
  );
  const unitOf = (material: string) => (lots.data ?? []).find((lot) => lot.material === material)?.unit ?? '';

  const checks = useMemo(
    () =>
      draft
        ? editorChecks(
            draft.steps,
            draft.bom,
            draft.meta.risk,
            stations.data,
            capabilityIndex,
            sopLabel(sops.data, draft.meta.sop_version_id),
            recipes.data ? subflowIndex : undefined,
            recipeId,
          )
        : [],
    [draft, stations.data, capabilityIndex, sops.data, subflowIndex, recipes.data, recipeId],
  );

  if (!recipe.data || !draft) {
    return <div className="boot">{recipe.error ? recipe.error.message : '加载中…'}</div>;
  }

  const data = recipe.data;
  const readOnlyWhy =
    data.state !== 'draft'
      ? `当前状态「${data.state_label}」不可编辑，请在已发布版本上新建修订草稿`
      : !can('recipe.edit')
        ? '当前角色不能编辑配方'
        : '';
  const readOnly = !!readOnlyWhy;
  const ids = draft.steps.map(stepIdOf);
  const selectedIndex = selected ? ids.indexOf(selected) : -1;

  /* ---------- 编辑动作 ---------- */

  const mutate = (change: (current: Draft) => Draft) => {
    setDraft((current) => (current ? change(clone(current)) : current));
    setDirty(true);
  };

  /** 新步骤：依赖图模式下接在选中的节点之后（选中的是分支就占它下一个空闲出口）；顺序流程插在选中行之后。 */
  const insert = (raw: RecipeStep, after: string | null = selected) => {
    const step: RecipeStep = { ...raw, step_id: nextStepId(draft.steps, data.used_step_ids) };
    const anchorIndex = after ? ids.indexOf(after) : -1;
    const anchor = anchorIndex >= 0 ? draft.steps[anchorIndex] : null;
    const toGraph = graphMode(draft.steps) || kindOf(step) === 'branch' || (anchor && kindOf(anchor) === 'branch');
    mutate((current) => {
      if (!toGraph) {
        const at = anchorIndex >= 0 ? anchorIndex + 1 : current.steps.length;
        current.steps.splice(at, 0, step);
        return current;
      }
      const steps = explicitAfter(current.steps);
      const added: RecipeStep = { ...step, after: anchor ? [after as string] : [] };
      if (anchor && kindOf(anchor) === 'branch') added.when = { [after as string]: freeCase(steps, after as string) };
      current.steps = topoSort([...steps, added]);
      return current;
    });
    setSelected(step.step_id as string);
  };

  const addDevice = (capability: CapabilityRow, after: string | null = selected) =>
    insert(
      { kind: 'device', name: capability.name, cap: capability.id, params: defaultParams(stations.data, capability), dur: 15 },
      after,
    );

  const addFromPalette = (payload: string, onto: string | null) => {
    const [type, value] = payload.split(':');
    if (type === 'cap') {
      const capability = capabilityIndex[value];
      if (capability) addDevice(capability, onto);
      return;
    }
    insert(blankStep(value as Exclude<StepKind, 'device'>), onto);
  };

  /** 建依赖 from → to。成环、重复都拒绝；改完按拓扑序重排列表。 */
  const connect = (from: string, to: string) => {
    if (readOnly) return;
    if (wouldCycle(draft.steps, from, to)) {
      toast.push('这条依赖会形成环：目标步骤已经在它的上游。回环请用条件分支的「回到」出口');
      return;
    }
    const target = draft.steps[ids.indexOf(to)];
    if (graphMode(draft.steps) && predecessors(draft.steps)[ids.indexOf(to)].includes(ids.indexOf(from))) {
      toast.push('已经有这条依赖');
      return;
    }
    const source = draft.steps[ids.indexOf(from)];
    mutate((current) => {
      const steps = explicitAfter(current.steps);
      const at = steps.findIndex((step, index) => stepIdOf(step, index) === to);
      steps[at] = { ...steps[at], after: [...new Set([...(steps[at].after ?? []), from])] };
      if (kindOf(source) === 'branch') steps[at].when = { ...whenOf(target), [from]: freeCase(steps, from) };
      current.steps = topoSort(steps);
      return current;
    });
  };

  const disconnect = (from: string, to: string) => {
    if (readOnly) return;
    mutate((current) => {
      const steps = explicitAfter(current.steps);
      const at = steps.findIndex((step, index) => stepIdOf(step, index) === to);
      const when = { ...whenOf(steps[at]) };
      delete when[from];
      steps[at] = { ...steps[at], after: (steps[at].after ?? []).filter((ref) => ref !== from), when };
      if (!Object.keys(when).length) delete steps[at].when;
      current.steps = topoSort(steps);
      return current;
    });
  };

  const setStep = (id: string, change: (step: RecipeStep) => void) =>
    mutate((current) => {
      const at = current.steps.findIndex((step, index) => stepIdOf(step, index) === id);
      if (at >= 0) change(current.steps[at]);
      return current;
    });

  /** 顺序流程里前移 / 后移就是改执行顺序；依赖图里顺序由依赖决定，不提供。 */
  const moveStep = (from: number, to: number) => {
    if (to < 0 || to >= draft.steps.length || from === to) return;
    mutate((current) => {
      const [step] = current.steps.splice(from, 1);
      current.steps.splice(to, 0, step);
      return current;
    });
  };

  const duplicateStep = (id: string) => {
    const at = ids.indexOf(id);
    const copy: RecipeStep = { ...clone(draft.steps[at]), step_id: nextStepId(draft.steps, data.used_step_ids) };
    mutate((current) => {
      current.steps.splice(at + 1, 0, copy);
      if (graphMode(current.steps)) current.steps = topoSort(current.steps);
      return current;
    });
    setSelected(copy.step_id as string);
  };

  /** 删除节点。依赖图里它的后继改接到它的前驱上，出口条件一并继承，不留断头的路。 */
  const deleteStep = (id: string) => {
    const at = ids.indexOf(id);
    mutate((current) => {
      if (!graphMode(current.steps)) {
        current.steps.splice(at, 1);
        return current;
      }
      const steps = explicitAfter(current.steps);
      const removed = steps[at];
      const rest = steps.filter((_, index) => index !== at).map((step) => {
        if (!(step.after ?? []).includes(id)) return step;
        const when = { ...whenOf(step) };
        delete when[id];
        Object.entries(whenOf(removed)).forEach(([branch, key]) => (when[branch] ??= key));
        const after = [...new Set([...(step.after ?? []).filter((ref) => ref !== id), ...(removed.after ?? [])])];
        const next: RecipeStep = { ...step, after, when };
        if (!Object.keys(when).length) delete next.when;
        return next;
      });
      current.steps = topoSort(rest);
      return current;
    });
    const remaining = ids.filter((value) => value !== id);
    setSelected(remaining[Math.min(at, remaining.length - 1)] ?? null);
  };

  const changeCapability = (id: string, capabilityId: string) => {
    const capability = capabilityIndex[capabilityId];
    if (!capability) return;
    setStep(id, (step) => {
      const wasDefaultName = step.name === capabilityIndex[step.cap]?.name;
      // 换了能力，原来引用的设备方法就不适用了
      if (step.cap !== capabilityId) delete step.method;
      step.cap = capabilityId;
      step.params = defaultParams(stations.data, capability);
      if (wasDefaultName) step.name = capability.name;
    });
  };

  const toggleHard = (id: string, on: boolean) => {
    const at = ids.indexOf(id);
    const previous = predecessors(draft.steps)[at]?.[0];
    setStep(id, (step) => {
      if (on) {
        const before = previous !== undefined ? draft.steps[previous] : undefined;
        step.hard = { from: before ? `${before.name}结束` : '托盘就位', maxGapMin: 30 };
      } else {
        delete step.hard;
      }
    });
  };

  const discard = () => {
    setDraft({
      steps: clone(data.steps ?? []),
      bom: clone(data.bom ?? []),
      meta: {
        name: data.name, plate: data.plate, design: data.design, risk: data.risk,
        sop_version_id: data.sop_version_id ?? '',
      },
    });
    setSelected(data.steps?.length ? stepIdOf(data.steps[0], 0) : null);
    setDirty(false);
    toast.push('已放弃未保存的更改');
  };

  const persist = async (thenSubmit: boolean) => {
    const payload = {
      name: draft.meta.name,
      plate: draft.meta.plate === '' ? 1 : draft.meta.plate,
      design: draft.meta.design,
      risk: draft.meta.risk,
      sop_version_id: draft.meta.sop_version_id,
      steps: draft.steps,
      bom: draft.bom,
      // 别人在此期间改过草稿时服务端返回 409，不静默覆盖
      row_version: data.row_version,
    };
    try {
      const saved = await save.run(payload);
      loadedFor.current = '';
      setDirty(false);
      if (!thenSubmit) {
        toast.push(`草稿已保存，服务端校验${saved.valid ? '通过' : '未通过'}`);
        return;
      }
      await submit.run();
      toast.push('已提交评审，服务端能力校验通过');
      navigate(`/recipes/${recipeId}`);
    } catch (error) {
      toast.push((error as Error).message);
    }
  };

  /* ---------- 流程图 ---------- */

  const before = predecessors(draft.steps);
  const nodes: FlowGraphNode[] = draft.steps.map((step, index) => {
    const id = ids[index];
    const issues = stepIssues(step, capabilityIndex, draft.steps, index, recipes.data ? subflowIndex : undefined, recipeId);
    const fits = stationsForStep(stations.data, step).map((station) => station.id);
    const requiresStation = needsStation(step);
    const bad = issues.length > 0 || (requiresStation && fits.length === 0);
    return {
      id,
      index,
      className: `k-${kindOf(step)}${bad ? ' bad' : ''}`,
      title: bad ? [...issues, ...(requiresStation && !fits.length ? ['没有工位能承接这些参数'] : [])].join('；') : '点击编辑属性',
      content: (
        <NodeCard
          step={step}
          index={index}
          fits={fits}
          capabilityName={capabilityIndex[step.cap]?.name ?? step.cap}
          paramLabels={capabilityIndex[step.cap]?.params ?? {}}
          subflowName={subflowIndex[step.subflow?.recipe_id ?? '']?.name}
          duration={durationOf(step, subflowIndex)}
        />
      ),
    };
  });
  const edges: FlowGraphEdge[] = draft.steps.flatMap((step, index) =>
    before[index].map((parent) => {
      const parentStep = draft.steps[parent];
      const branchKey = whenOf(step)[ids[parent]];
      if (kindOf(parentStep) === 'branch') {
        const label = branchCases(parentStep).find((row) => row.key === branchKey)?.label;
        return { from: ids[parent], to: ids[index], tone: 'branch' as const, label: label ?? '未指定出口' };
      }
      if (step.hard?.maxGapMin) {
        return {
          from: ids[parent], to: ids[index], tone: 'hard' as const, label: `≤${step.hard.maxGapMin} min`,
          title: `自${step.hard.from}起 ${step.hard.maxGapMin} min 内必须开始`,
        };
      }
      return { from: ids[parent], to: ids[index] };
    }),
  );
  const loops: FlowGraphLoop[] = draft.steps.flatMap((step, index) =>
    kindOf(step) === 'branch'
      ? branchCases(step)
          .filter((row) => row.loop_to && ids.includes(row.loop_to))
          .map((row) => ({ from: ids[index], to: row.loop_to as string, label: `${row.label}（≤${step.branch?.max_loops ?? '?'} 次）` }))
      : [],
  );

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>
            {dirty ? <span className="dirty-dot" title="有未保存更改" /> : null}
            图形化编辑 · {draft.meta.name} <Pill state={data.state} label={data.state_label} />
          </h1>
          <div className="small muted">
            <Link to="/recipes">配方</Link> / <Link to={`/recipes/${data.id}`}>{data.id}</Link> / 编辑 ·{' '}
            <span className="mono">v{data.version}</span>
          </div>
        </div>
        <div className="row">
          <Link className="btn" to={`/recipes/${data.id}`}>
            返回详情
          </Link>
          <button className="btn" disabled={!dirty} onClick={discard}>
            放弃更改
          </button>
          <button
            className="btn"
            disabled={readOnly || !dirty || save.pending}
            title={readOnlyWhy || (dirty ? undefined : '没有更改')}
            onClick={() => persist(false)}
          >
            保存草稿
          </button>
          <button
            className="btn primary"
            disabled={readOnly || !canSubmit(checks) || !can('recipe.submit') || save.pending || submit.pending}
            title={
              readOnlyWhy ||
              (!canSubmit(checks) ? '校验未通过' : !can('recipe.submit') ? '当前角色不能提交评审' : undefined)
            }
            onClick={() => persist(true)}
          >
            保存并提交评审
          </button>
        </div>
      </div>

      {readOnly ? (
        <div className="note warn">只读：{readOnlyWhy}。</div>
      ) : (
        <div className="note">
          从左侧拖一个节点到画布上（落在某个节点上就接在它后面），或选中节点后点「添加」。
          按住节点右侧的圆点拖到另一个节点上即建立依赖：一个节点连出多条就是并行，多条连进一个节点就是汇合。
          条件分支的每条出边要指定出口，回环在分支的出口里设「回到」。这里的校验是即时提示，能不能提交由服务端重算。
        </div>
      )}

      <div className="designer">
        <Panel title="节点面板">
          <div className="palette">
            <div className="cap">
              <div className="cap-head">
                <b>流程节点</b>
              </div>
              <div className="small muted">不绑定能力、默认不占工位；可拖到画布上。</div>
              <div className="filters" style={{ marginTop: 6 }}>
                {NON_DEVICE.map(([kind, label]) => (
                  <button
                    key={kind}
                    className="btn sm"
                    disabled={readOnly}
                    draggable={!readOnly}
                    onDragStart={(event) => event.dataTransfer.setData(PALETTE_TYPE, `kind:${kind}`)}
                    onClick={() => insert(blankStep(kind))}
                  >
                    + {label}
                  </button>
                ))}
              </div>
            </div>
            {(capabilities.data ?? []).map((capability) => (
              <div
                key={capability.id}
                className="cap"
                draggable={!readOnly}
                onDragStart={(event) => event.dataTransfer.setData(PALETTE_TYPE, `cap:${capability.id}`)}
              >
                <div className="cap-head">
                  <b>{capability.name}</b>
                  <button className="btn sm" disabled={readOnly} onClick={() => addDevice(capability)}>
                    添加
                  </button>
                </div>
                <div className="small muted">{Object.values(capability.params).join(' · ') || '无参数'}</div>
                <div className="tiny muted">
                  {capability.recovery.pausable ? '可保持' : '不可保持'} ·{' '}
                  {capability.recovery.retryable ? '可重试' : '不可重试'} ·{' '}
                  <span className="mono">{capability.stations.join(' ') || '无实现工位'}</span>
                </div>
              </div>
            ))}
          </div>
        </Panel>

        <div className="grid">
          <Panel
            title={`流程图 · ${draft.steps.length} 步`}
            aside={<span className="small muted">{graphMode(draft.steps) ? '依赖图' : '顺序流程'}</span>}
            flush
          >
            <FlowGraph
              nodes={nodes}
              edges={edges}
              loops={loops}
              selected={selected}
              editable={!readOnly}
              onSelect={setSelected}
              onConnect={connect}
              onRemoveEdge={disconnect}
              onDropPalette={addFromPalette}
              empty="从左侧拖入或点击添加节点。设备步骤绑定能力而不是设备，参数对照全部工位的极限校验；人工、等待、审核节点不占工位。"
            />
          </Panel>
          <Panel title="校验">
            <CheckList checks={checks} />
            <div className="small muted">前五项通过才能提交评审；风险评估编号可在发布前补齐。</div>
          </Panel>
        </div>

        {selectedIndex >= 0 ? (
          <StepProperties
            index={selectedIndex}
            step={draft.steps[selectedIndex]}
            steps={draft.steps}
            recipeId={recipeId}
            recipes={recipes.data ?? []}
            subflows={subflowIndex}
            capabilities={capabilities.data ?? []}
            capabilityIndex={capabilityIndex}
            stations={stations.data}
            methods={methods.data ?? []}
            readOnly={readOnly}
            onBack={() => setSelected(null)}
            onSet={(change) => setStep(ids[selectedIndex], change)}
            onCapability={(id) => changeCapability(ids[selectedIndex], id)}
            onHard={(on) => toggleHard(ids[selectedIndex], on)}
            onMove={(to) => moveStep(selectedIndex, to)}
            onConnect={(from) => connect(from, ids[selectedIndex])}
            onDisconnect={(from) => disconnect(from, ids[selectedIndex])}
            onDuplicate={() => duplicateStep(ids[selectedIndex])}
            onDelete={() => deleteStep(ids[selectedIndex])}
          />
        ) : (
          <RecipeProperties
            meta={draft.meta}
            bom={draft.bom}
            materials={materials}
            unitOf={unitOf}
            readOnly={readOnly}
            sops={sops.data}
            onMeta={(key, value) =>
              mutate((current) => {
                (current.meta as Record<string, unknown>)[key] = value;
                return current;
              })
            }
            onBom={(rows) => mutate((current) => ({ ...current, bom: rows }))}
          />
        )}
      </div>
    </div>
  );
}

/* ---------- 画布节点 ---------- */

function NodeCard({
  step,
  index,
  fits,
  capabilityName,
  paramLabels,
  subflowName,
  duration,
}: {
  step: RecipeStep;
  index: number;
  fits: string[];
  capabilityName: string;
  paramLabels: Record<string, string>;
  subflowName?: string;
  duration: number;
}) {
  const kind = kindOf(step);
  const requiresStation = needsStation(step);
  const meta =
    kind === 'device'
      ? Object.entries(step.params ?? {})
          .map(([key, value]) => `${(paramLabels[key] ?? key).split(' ')[0]} ${value === '' ? '?' : value}`)
          .join(' · ') || '无参数'
      : kind === 'manual'
      ? `${(step.form ?? []).length} 个记录字段`
      : kind === 'wait'
      ? step.wait_for?.mode === 'event'
        ? `等待事件 ${step.wait_for.event || '未填写'}`
        : '定时等待'
      : kind === 'gate'
      ? `${step.gate?.field || '未选字段'} ${step.gate?.min ?? '−∞'}…${step.gate?.max ?? '+∞'} · ${
          { rework: '返工', scrap: '报废', hold: '保持' }[step.gate?.on_fail ?? 'hold']
        }`
      : kind === 'split'
      ? `每样本拆 ${step.split?.count ?? '?'} 个${step.split?.child_type || ''}`
      : kind === 'branch'
      ? `${{ measure: '按测量值', form: '按记录字段', manual: '人工选择' }[step.branch?.mode ?? 'manual']} · ${branchCases(step)
          .map((row) => row.label || row.key)
          .join(' / ')}`
      : kind === 'subflow'
      ? subflowName ? `引用 ${step.subflow?.recipe_id} ${subflowName}` : '未选择引用的方法'
      : kind === 'notify'
      ? step.notify?.message || '未填写通知内容'
      : `审核角色 ${step.review_role || 'qa'}`;
  return (
    <>
      <span className="fn-head">
        <span className="mono">{index + 1}</span>
        <span className={`kind ${kind}`}>{STEP_KINDS.find(([value]) => value === kind)?.[1] ?? kind}</span>
        {kind === 'device' ? <span className="tag">{capabilityName}</span> : null}
        {step.skippable ? <span className="tag" title="运行时允许跳过">可跳过</span> : null}
        {step.timeout ? <span className="tag warn" title="步骤级超时">⏱{step.timeout.minutes}</span> : null}
      </span>
      <span className="fn-title">{step.name || <span className="muted">未命名</span>}</span>
      <span className="fn-meta">{meta}</span>
      <span className="fn-foot">
        <span className="mono">
          {kind === 'subflow' ? `${duration} min` : AUTOMATIC_KINDS.includes(kind) ? '—' : `${step.dur} min`}
        </span>
        {requiresStation ? (
          <span className={fits.length ? 'muted' : 'bad'}>{fits.length ? fits.join(' ') : '无可承接工位'}</span>
        ) : (
          <span className="muted">不占工位</span>
        )}
      </span>
    </>
  );
}

/* ---------- 属性面板 ---------- */

function StepProperties({
  index,
  step,
  steps,
  recipeId,
  recipes,
  subflows,
  capabilities,
  capabilityIndex,
  stations,
  methods,
  readOnly,
  onBack,
  onSet,
  onCapability,
  onHard,
  onMove,
  onConnect,
  onDisconnect,
  onDuplicate,
  onDelete,
}: {
  index: number;
  step: RecipeStep;
  steps: RecipeStep[];
  recipeId: string;
  recipes: RecipeSummary[];
  subflows: SubflowIndex;
  capabilities: CapabilityRow[];
  capabilityIndex: Record<string, CapabilityRow>;
  stations: StationRow[] | undefined;
  methods: DeviceMethodRow[];
  readOnly: boolean;
  onBack: () => void;
  onSet: (change: (step: RecipeStep) => void) => void;
  onCapability: (id: string) => void;
  onHard: (on: boolean) => void;
  onMove: (to: number) => void;
  onConnect: (from: string) => void;
  onDisconnect: (from: string) => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const capability = capabilityIndex[step.cap];
  const recovery = capability?.recovery ?? {};
  const fits = stationsForStep(stations, step);
  const kind = kindOf(step);
  const upstream = predecessors(steps)[index].map((at) => steps[at]);
  const timeoutActions = TIMEOUT_ACTIONS_BY_KIND[kind];

  return (
    <Panel title={`第 ${index + 1} 步 · ${stepIdOf(step, index)}`} aside={<button className="btn sm" onClick={onBack}>方法属性</button>}>
      <Field label="步骤名称">
        <input
          value={step.name}
          readOnly={readOnly}
          onChange={(event) => onSet((current) => void (current.name = event.target.value))}
        />
      </Field>

      <Field label="步骤类型" hint="不同类型适用不同字段；界面只显示适用的那些">
        <select
          value={kind}
          disabled={readOnly}
          onChange={(event) =>
            onSet((current) => {
              const next = event.target.value as StepKind;
              current.kind = next;
              if (next !== 'device') {
                current.cap = '';
                current.params = {};
              }
              const blank = next === 'device' ? null : blankStep(next);
              if (next === 'manual' && !current.form?.length) current.form = blank?.form;
              if (next === 'wait' && !current.wait_for) current.wait_for = { mode: 'duration' };
              if (next === 'review' && !current.review_role) current.review_role = 'qa';
              if (next === 'gate' && !current.gate) current.gate = blank?.gate;
              if (next === 'split' && !current.split) current.split = blank?.split;
              if (next === 'branch' && !current.branch) current.branch = blank?.branch;
              if (next === 'subflow' && !current.subflow) current.subflow = { recipe_id: '' };
              if (next === 'notify' && !current.notify) current.notify = { message: '' };
              if (!SKIPPABLE_KINDS.includes(next)) delete current.skippable;
              if (!TIMEOUT_ACTIONS_BY_KIND[next]) delete current.timeout;
            })
          }
        >
          {STEP_KINDS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </Field>

      {kind === 'device' ? (
        <Field label="能力" hint="更换能力后参数重置为各工位极限的中值">
          <select value={step.cap} disabled={readOnly} onChange={(event) => onCapability(event.target.value)}>
            <option value="">选择能力</option>
            {capabilities.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
        </Field>
      ) : null}

      {kind === 'device' ? (
        <MethodField step={step} methods={methods} readOnly={readOnly} onSet={onSet} />
      ) : null}

      {kind === 'manual' ? <ManualFields step={step} readOnly={readOnly} onSet={onSet} /> : null}

      {kind === 'wait' ? (
        <>
          <Field label="等待方式" hint="业务事件由批次页或外部系统（服务身份）发出，早到的事件会先登记">
            <select
              value={step.wait_for?.mode ?? 'duration'}
              disabled={readOnly}
              onChange={(event) =>
                onSet((current) => {
                  const mode = event.target.value as 'duration' | 'event';
                  current.wait_for = { ...current.wait_for, mode };
                  if (mode === 'duration') delete current.timeout;
                })
              }
            >
              <option value="duration">固定时长</option>
              <option value="event">业务事件</option>
            </select>
          </Field>
          {step.wait_for?.mode === 'event' ? (
            <Field label="事件名" hint="如 sample_received、qc_released；外部系统按这个名字发信号">
              <input
                value={step.wait_for?.event ?? ''}
                readOnly={readOnly}
                onChange={(event) => onSet((current) => void (current.wait_for = { ...current.wait_for, event: event.target.value }))}
              />
            </Field>
          ) : null}
        </>
      ) : null}

      {kind === 'gate' ? <GateFields step={step} steps={steps} index={index} readOnly={readOnly} onSet={onSet} /> : null}
      {kind === 'branch' ? <BranchFields step={step} steps={steps} index={index} readOnly={readOnly} onSet={onSet} /> : null}
      {kind === 'subflow' ? (
        <SubflowFields step={step} recipeId={recipeId} recipes={recipes} subflows={subflows} readOnly={readOnly} onSet={onSet} />
      ) : null}
      {kind === 'notify' ? (
        <>
          <Field label="通知内容" hint="发一条 flow.notify 对外事件，订阅方（如 IM 机器人、LIMS）收到后处理；节点立即完成">
            <textarea
              rows={3}
              value={step.notify?.message ?? ''}
              readOnly={readOnly}
              onChange={(event) => onSet((current) => void (current.notify = { ...current.notify, message: event.target.value }))}
            />
          </Field>
          <Field label="渠道标识（可选）" hint="原样放进事件载荷，接收方据此路由，如 qa-group">
            <input
              value={step.notify?.channel ?? ''}
              readOnly={readOnly}
              onChange={(event) => onSet((current) => void (current.notify = { ...current.notify, channel: event.target.value }))}
            />
          </Field>
        </>
      ) : null}
      {kind === 'split' ? (
        <div className="grid cols-2">
          <Field label="每个样本拆分份数" hint="如一瓶电解液做 4 个扣电">
            <NumberInput
              value={step.split?.count ?? ''}
              disabled={readOnly}
              ariaLabel="拆分份数"
              onChange={(next) => onSet((current) => void (current.split = { ...current.split, count: next === '' ? undefined : next }))}
            />
          </Field>
          <Field label="子样本类型">
            <input
              value={step.split?.child_type ?? ''}
              readOnly={readOnly}
              placeholder="扣电 / 极片"
              onChange={(event) => onSet((current) => void (current.split = { ...current.split, child_type: event.target.value }))}
            />
          </Field>
        </div>
      ) : null}

      {kind === 'review' ? (
        <Field label="审核角色" hint="批准才继续；本人不能审核本人提交的上游记录">
          <select
            value={step.review_role ?? 'qa'}
            disabled={readOnly}
            onChange={(event) => onSet((current) => void (current.review_role = event.target.value))}
          >
            <option value="qa">QA</option>
            <option value="researcher">研究员</option>
            <option value="admin">系统管理员</option>
          </select>
        </Field>
      ) : null}

      {kind === 'device' ? (
        <Field label="消耗物料">
          <label className="small">
            <input
              type="checkbox"
              checked={Boolean(step.consumes_materials)}
              disabled={readOnly}
              onChange={(event) => onSet((current) => void (current.consumes_materials = event.target.checked))}
            />
            该步骤消耗 BOM 物料
          </label>
        </Field>
      ) : null}

      {kind !== 'device' ? null : Object.entries(capability?.params ?? {}).map(([key, paramLabel]) => {
        const range = paramRange(stations, step.cap, key);
        const value = step.params?.[key];
        const rule = step.method?.params?.[key];
        const inMethod =
          !rule || typeof value !== 'number' || ((rule.min == null || value >= rule.min) && (rule.max == null || value <= rule.max));
        const ok = !!range && typeof value === 'number' && value >= range[0] && value <= range[1] && inMethod;
        return (
          <Field
            key={key}
            label={`${paramLabel}　${range ? `[${range[0]}, ${range[1]}]` : '无工位定义该参数'}`}
            hint={rule ? `设备方法允许 [${rule.min ?? '−∞'}, ${rule.max ?? '∞'}]${rule.unit ? ` ${rule.unit}` : ''}，缺省 ${rule.default ?? '—'}` : undefined}
          >
            <NumberInput
              value={value ?? ''}
              invalid={!ok}
              disabled={readOnly}
              ariaLabel={paramLabel}
              onChange={(next) => onSet((current) => void (current.params[key] = next))}
            />
          </Field>
        );
      })}
      {capability && !Object.keys(capability.params ?? {}).length ? (
        <div className="small muted">该能力无参数</div>
      ) : null}

      {!AUTOMATIC_KINDS.includes(kind) ? (
        <Field label="计划时长 min" hint="仅用于排程；实际终点由设备事件决定">
          <NumberInput
            value={step.dur}
            invalid={!(step.dur > 0)}
            disabled={readOnly}
            ariaLabel="计划时长"
            onChange={(next) => onSet((current) => void (current.dur = next === '' ? 0 : next))}
          />
        </Field>
      ) : null}

      <DependencyEditor steps={steps} index={index} readOnly={readOnly} onConnect={onConnect} onDisconnect={onDisconnect} />
      {upstream.some((row) => kindOf(row) === 'branch') ? (
        <WhenFields step={step} steps={steps} index={index} readOnly={readOnly} onSet={onSet} />
      ) : null}

      {timeoutActions ? (
        <TimeoutFields step={step} actions={timeoutActions} readOnly={readOnly} onSet={onSet} />
      ) : null}
      {SKIPPABLE_KINDS.includes(kind) ? (
        <label className="check" title="方法作者在设计时同意：运行时可由有恢复权限的人签名跳过这一步">
          <input
            type="checkbox"
            checked={Boolean(step.skippable)}
            disabled={readOnly}
            onChange={(event) =>
              onSet((current) => {
                if (event.target.checked) current.skippable = true;
                else delete current.skippable;
              })
            }
          />
          运行时允许跳过（非关键步骤；跳过要写理由并签名）
        </label>
      ) : null}

      {!AUTOMATIC_KINDS.includes(kind) ? (
        <label className="check">
          <input
            type="checkbox"
            checked={!!step.hard}
            disabled={readOnly}
            onChange={(event) => onHard(event.target.checked)}
          />
          硬时限：本步必须在上一事件后的限定时间内开始
        </label>
      ) : null}

      {step.hard ? (
        <div className="grid cols-2">
          <Field label="起算事件">
            <input
              value={step.hard.from}
              readOnly={readOnly}
              placeholder="上一步结束"
              onChange={(event) => onSet((current) => void (current.hard!.from = event.target.value))}
            />
          </Field>
          <Field label="最长间隔 min">
            <NumberInput
              value={step.hard.maxGapMin}
              invalid={!(step.hard.maxGapMin > 0)}
              disabled={readOnly}
              ariaLabel="最长间隔"
              onChange={(next) => onSet((current) => void (current.hard!.maxGapMin = next === '' ? 0 : next))}
            />
          </Field>
        </div>
      ) : null}

      {needsStation(step) ? (
        <div className={`small ${fits.length ? 'muted' : 'bad-text'}`}>
          {fits.length ? (
            <>
              可承接工位：
              {fits.map((station) => (
                <span key={station.id} className="tag">
                  {station.id}
                </span>
              ))}
            </>
          ) : (
            '没有工位的能力极限能覆盖这些参数'
          )}
        </div>
      ) : null}

      {kind === 'device' ? (
        <div className="small muted">
          恢复规则（继承自能力，配方不可覆盖）：
          {recovery.pausable ? `可保持 ≤ ${recovery.maxHoldMin} min，${recovery.hold}` : '不可保持'} ·{' '}
          {recovery.retryable ? '可重试' : '不可重试'}
          {recovery.verify?.length ? ` · 恢复前核实 ${recovery.verify.join('、')}` : ''}
        </div>
      ) : null}

      <div className="actions">
        {!graphMode(steps) ? (
          <>
            <button className="btn sm" disabled={readOnly || index === 0} onClick={() => onMove(index - 1)}>
              ◀ 前移
            </button>
            <button className="btn sm" disabled={readOnly || index === steps.length - 1} onClick={() => onMove(index + 1)}>
              后移 ▶
            </button>
          </>
        ) : null}
        <button className="btn sm" disabled={readOnly} onClick={onDuplicate}>
          复制
        </button>
        <button className="btn sm danger" disabled={readOnly} onClick={onDelete}>
          删除
        </button>
      </div>
    </Panel>
  );
}

function ManualFields({
  step,
  readOnly,
  onSet,
}: {
  step: RecipeStep;
  readOnly: boolean;
  onSet: (change: (step: RecipeStep) => void) => void;
}) {
  return (
    <>
      <Field label="记录表单" hint="人工步骤必须有结构化记录；缺必填项时服务端不推进">
        <div className="stack">
          {(step.form ?? []).map((field, position) => (
            <div className="filters" key={position}>
              <input
                placeholder="字段标识"
                value={field.key}
                readOnly={readOnly}
                onChange={(event) => onSet((current) => void (current.form![position].key = event.target.value))}
              />
              <input
                placeholder="显示名称"
                value={field.label}
                readOnly={readOnly}
                onChange={(event) => onSet((current) => void (current.form![position].label = event.target.value))}
              />
              <select
                value={field.type ?? 'text'}
                disabled={readOnly}
                onChange={(event) =>
                  onSet((current) => void (current.form![position].type = event.target.value as FormField['type']))
                }
              >
                <option value="number">数值</option>
                <option value="text">文本</option>
                <option value="bool">勾选</option>
                <option value="enum">枚举</option>
              </select>
              <label className="tiny">
                <input
                  type="checkbox"
                  checked={field.required !== false}
                  disabled={readOnly}
                  onChange={(event) => onSet((current) => void (current.form![position].required = event.target.checked))}
                />
                必填
              </label>
              <button
                className="btn sm"
                disabled={readOnly}
                onClick={() => onSet((current) => void current.form!.splice(position, 1))}
              >
                移除
              </button>
            </div>
          ))}
          <button
            className="btn sm"
            disabled={readOnly}
            onClick={() =>
              onSet((current) => {
                current.form = [...(current.form ?? []), { key: '', label: '', type: 'text', required: true }];
              })
            }
          >
            增加字段
          </button>
        </div>
      </Field>
      <Field label="其他要求">
        <label className="small">
          <input
            type="checkbox"
            checked={Boolean(step.requires_signature)}
            disabled={readOnly}
            onChange={(event) => onSet((current) => void (current.requires_signature = event.target.checked))}
          />
          提交需要电子签名
        </label>
        <label className="small">
          <input
            type="checkbox"
            checked={Boolean(step.consumes_materials)}
            disabled={readOnly}
            onChange={(event) => onSet((current) => void (current.consumes_materials = event.target.checked))}
          />
          该步骤消耗 BOM 物料（勾了才要求 BOM 与投料许可）
        </label>
      </Field>
    </>
  );
}

function RecipeProperties({
  meta,
  bom,
  materials,
  unitOf,
  readOnly,
  sops,
  onMeta,
  onBom,
}: {
  meta: Meta;
  bom: BomItem[];
  materials: string[];
  unitOf: (material: string) => string;
  readOnly: boolean;
  sops: SopVersionRow[] | undefined;
  onMeta: (key: keyof Meta, value: string | number | '') => void;
  onBom: (rows: BomItem[]) => void;
}) {
  const update = (index: number, change: Partial<BomItem>) =>
    onBom(bom.map((item, order) => (order === index ? { ...item, ...change } : item)));
  const selectedSop = (sops ?? []).find((row) => row.id === meta.sop_version_id);

  return (
    <Panel title="配方属性">
      <Field label="名称">
        <input value={meta.name} readOnly={readOnly} onChange={(event) => onMeta('name', event.target.value)} />
      </Field>
      <div className="grid cols-2">
        <Field label="每批样品位">
          <NumberInput
            value={meta.plate}
            disabled={readOnly}
            invalid={!(Number(meta.plate) >= 1 && Number(meta.plate) <= 96)}
            ariaLabel="每批样品位"
            onChange={(next) => onMeta('plate', next)}
          />
        </Field>
        <Field label="风险评估编号">
          <input
            value={meta.risk}
            readOnly={readOnly}
            placeholder="RA-xxx vN"
            onChange={(event) => onMeta('risk', event.target.value)}
          />
        </Field>
      </div>
      <Field
        label="关联受控 SOP"
        hint="只能选已发布且生效的版本；下发批次时会按它校验培训确认，并把版本与校验和写进批次快照"
      >
        <select
          value={meta.sop_version_id}
          disabled={readOnly}
          onChange={(event) => onMeta('sop_version_id', event.target.value)}
        >
          <option value="">不关联</option>
          {(sops ?? []).map((row) => (
            <option key={row.id} value={row.id}>
              {row.code} {row.title} · {row.version}
              {row.requires_training_ack ? '（需培训确认）' : ''}
            </option>
          ))}
        </select>
      </Field>
      {meta.sop_version_id && sops && !sops.some((row) => row.id === meta.sop_version_id) ? (
        <div className="note">
          当前关联的版本不在有效清单里（可能已退役或被新版本替代）。历史批次仍指向它固化的快照，
          但新批次会按这条引用校验——要么改到新版本，要么在 SOP 页把它重新发布。
        </div>
      ) : null}
      {selectedSop?.requires_training_ack ? (
        <div className="note">
          这个版本要求培训确认：执行人没有确认记录时，批次下发会被开跑检查挡住。
        </div>
      ) : null}
      <Field label="实验设计说明">
        <textarea
          rows={3}
          value={meta.design}
          readOnly={readOnly}
          onChange={(event) => onMeta('design', event.target.value)}
        />
      </Field>

      <div>
        <div className="small muted" style={{ marginBottom: 6 }}>
          每批物料需求（BOM，排程前按此预留）
        </div>
        <table>
          <thead>
            <tr>
              <th>物料</th>
              <th className="num">数量</th>
              <th>单位</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {bom.map((item, index) => (
              <tr key={index}>
                <td>
                  {readOnly ? (
                    item.material
                  ) : (
                    <select
                      value={item.material}
                      onChange={(event) =>
                        update(index, { material: event.target.value, unit: unitOf(event.target.value) || item.unit })
                      }
                    >
                      {[...new Set([item.material, ...materials])].filter(Boolean).map((material) => (
                        <option key={material} value={material}>
                          {material}
                        </option>
                      ))}
                    </select>
                  )}
                </td>
                <td className="num">
                  {readOnly ? (
                    item.qty
                  ) : (
                    <NumberInput
                      value={item.qty}
                      ariaLabel={`${item.material} 数量`}
                      invalid={!(item.qty > 0)}
                      onChange={(next) => update(index, { qty: next === '' ? 0 : next })}
                    />
                  )}
                </td>
                <td className="mono">{item.unit}</td>
                <td className="row-end">
                  {readOnly ? null : (
                    <button
                      className="btn sm"
                      onClick={() => onBom(bom.filter((_, order) => order !== index))}
                      aria-label={`删除 ${item.material}`}
                    >
                      删
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {bom.length ? null : (
              <tr>
                <td colSpan={4} className="muted">
                  未定义物料，排程前无法预留
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {readOnly ? null : (
          <button
            className="btn sm"
            style={{ marginTop: 8 }}
            disabled={!materials.length}
            onClick={() => onBom([...bom, { material: materials[0], qty: 1, unit: unitOf(materials[0]) }])}
          >
            添加物料
          </button>
        )}
      </div>

      <div className="small muted">选中画布上的步骤可编辑它的参数与硬时限。</div>
    </Panel>
  );
}

/* 质检关卡：读取之前某个设备步骤回执里的测量值（如 KF 水分、黏度、面密度），按阈值自动判定。
   取不到数值不等于合格——一律转人工判断。 */
function GateFields({
  step,
  steps,
  index,
  readOnly,
  onSet,
}: {
  step: RecipeStep;
  steps: RecipeStep[];
  index: number;
  readOnly: boolean;
  onSet: (change: (step: RecipeStep) => void) => void;
}) {
  const gate = step.gate ?? {};
  const idOf = (row: RecipeStep, position: number) => row.step_id || `s${String(position + 1).padStart(2, '0')}`;
  const earlier = steps.slice(0, index).map((row, position) => ({ row, position, id: idOf(row, position) }));
  const set = (change: Partial<NonNullable<RecipeStep['gate']>>) =>
    onSet((current) => void (current.gate = { ...current.gate, ...change }));
  return (
    <>
      <div className="grid cols-2">
        <Field label="测量来源（之前的设备步骤）">
          <select value={gate.source_step_id ?? ''} disabled={readOnly} onChange={(event) => set({ source_step_id: event.target.value })}>
            <option value="">选择步骤</option>
            {earlier
              .filter(({ row }) => kindOf(row) === 'device')
              .map(({ row, position, id }) => (
                <option key={id} value={id}>
                  第 {position + 1} 步 · {row.name}
                </option>
              ))}
          </select>
        </Field>
        <Field label="测量字段" hint="设备回执 delivered 里的键，如 water_ppm">
          <input value={gate.field ?? ''} readOnly={readOnly} onChange={(event) => set({ field: event.target.value })} />
        </Field>
      </div>
      <div className="grid cols-3">
        <Field label="下限">
          <NumberInput
            value={gate.min ?? ''}
            disabled={readOnly}
            ariaLabel="下限"
            onChange={(next) => set({ min: next === '' ? null : next })}
          />
        </Field>
        <Field label="上限">
          <NumberInput
            value={gate.max ?? ''}
            disabled={readOnly}
            ariaLabel="上限"
            onChange={(next) => set({ max: next === '' ? null : next })}
          />
        </Field>
        <Field label="判定范围">
          <select value={gate.scope ?? 'batch'} disabled={readOnly} onChange={(event) => set({ scope: event.target.value as 'batch' | 'sample' })}>
            <option value="batch">整批（一个测量值）</option>
            <option value="sample">逐孔位（不合格样本单独剔除）</option>
          </select>
        </Field>
      </div>
      <div className="grid cols-3">
        <Field label="不合格去向">
          <select value={gate.on_fail ?? 'hold'} disabled={readOnly} onChange={(event) => set({ on_fail: event.target.value as 'rework' | 'scrap' | 'hold' })}>
            <option value="hold">保持，待 QA 判定</option>
            <option value="rework">返工</option>
            <option value="scrap">报废</option>
          </select>
        </Field>
        {gate.on_fail === 'rework' ? (
          <>
            <Field label="返工回到">
              <select value={gate.rework_to ?? ''} disabled={readOnly} onChange={(event) => set({ rework_to: event.target.value })}>
                <option value="">选择步骤</option>
                {earlier.map(({ row, position, id }) => (
                  <option key={id} value={id}>
                    第 {position + 1} 步 · {row.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="最多返工次数" hint="超过后转 QA 判定">
              <NumberInput
                value={gate.max_rework ?? ''}
                disabled={readOnly}
                ariaLabel="最多返工次数"
                onChange={(next) => set({ max_rework: next === '' ? undefined : next })}
              />
            </Field>
          </>
        ) : null}
      </div>
    </>
  );
}

/* 前驱步骤。勾选即连一条依赖（和在画布上拖线一样），取消即删掉；勾选多个即汇合。
   不能选本步的下游——那会成环；回环请用条件分支的「回到」出口。 */
function DependencyEditor({
  steps,
  index,
  readOnly,
  onConnect,
  onDisconnect,
}: {
  steps: RecipeStep[];
  index: number;
  readOnly: boolean;
  onConnect: (from: string) => void;
  onDisconnect: (from: string) => void;
}) {
  const graph = graphMode(steps);
  const current = predecessors(steps)[index];
  const downstream = descendants(steps, index);
  const candidates = steps
    .map((row, at) => ({ row, at, id: stepIdOf(row, at) }))
    .filter(({ at }) => at !== index && !downstream.has(at));
  return (
    <div className="deps">
      <div className="small">
        <b>前驱步骤</b>{' '}
        <span className="muted">
          {!graph ? '顺序流程：依赖上一步；勾选任意一项即改为依赖图' : current.length ? '汇合时等全部入边有结论' : '起点：不依赖任何步骤'}
        </span>
      </div>
      {candidates.length ? (
        <div className="dep-list">
          {candidates.map(({ row, at, id }) => (
            <label key={id} className="check">
              <input
                type="checkbox"
                disabled={readOnly}
                checked={current.includes(at)}
                onChange={(event) => (event.target.checked ? onConnect(id) : onDisconnect(id))}
              />
              {at + 1}. {row.name || '未命名'} <span className="tiny muted mono">{id}</span>
            </label>
          ))}
        </div>
      ) : (
        <div className="tiny muted">没有可以作为前驱的步骤。</div>
      )}
    </div>
  );
}

/* 本步在前驱分支的哪个出口上。每条来自分支的入边都要指定，否则推进器不知道该不该走这条路。 */
function WhenFields({
  step,
  steps,
  index,
  readOnly,
  onSet,
}: {
  step: RecipeStep;
  steps: RecipeStep[];
  index: number;
  readOnly: boolean;
  onSet: (change: (step: RecipeStep) => void) => void;
}) {
  const branches = predecessors(steps)[index].map((at) => ({ row: steps[at], id: stepIdOf(steps[at], at) }))
    .filter(({ row }) => kindOf(row) === 'branch');
  return (
    <div className="deps">
      <div className="small">
        <b>分支出口</b> <span className="muted">本步只在选中的出口被选中时执行，否则记为「未走此分支」</span>
      </div>
      {branches.map(({ row, id }) => (
        <Field key={id} label={`分支「${row.name}」`}>
          <select
            value={whenOf(step)[id] ?? ''}
            disabled={readOnly}
            onChange={(event) =>
              onSet((current) => void (current.when = { ...whenOf(current), [id]: event.target.value }))
            }
          >
            <option value="">未指定</option>
            {branchCases(row)
              .filter((c) => !c.loop_to)
              .map((c) => (
                <option key={c.key} value={c.key}>
                  {c.label || c.key}
                </option>
              ))}
          </select>
        </Field>
      ))}
    </div>
  );
}

/* 条件分支：按上游测量值 / 上游人工记录字段 / 人工选择取一个出口。出口按顺序匹配，第一个满足的生效；
   判据取不到值时走默认出口，没有默认出口就保持、等 QA 签名选择。出口可以「回到」上游某一步，
   就是有上限的循环。 */
function BranchFields({
  step,
  steps,
  index,
  readOnly,
  onSet,
}: {
  step: RecipeStep;
  steps: RecipeStep[];
  index: number;
  readOnly: boolean;
  onSet: (change: (step: RecipeStep) => void) => void;
}) {
  const config = step.branch ?? {};
  const mode = config.mode ?? 'manual';
  const cases = branchCases(step);
  const upstream = ancestors(steps, index);
  const earlier = steps
    .map((row, at) => ({ row, at, id: stepIdOf(row, at) }))
    .filter(({ at }) => upstream.has(at));
  const sources = earlier.filter(({ row }) => (mode === 'measure' ? kindOf(row) === 'device' : kindOf(row) === 'manual'));
  const source = earlier.find(({ id }) => id === config.source_step_id)?.row;
  const set = (change: Partial<NonNullable<RecipeStep['branch']>>) =>
    onSet((current) => void (current.branch = { ...current.branch, ...change }));
  const setCase = (position: number, change: Partial<BranchCase>) =>
    onSet((current) => {
      const rows = [...branchCases(current)];
      rows[position] = { ...rows[position], ...change };
      current.branch = { ...current.branch, cases: rows };
    });
  const numberOrNull = (value: number | '') => (value === '' ? null : value);
  const rowClass = mode === 'manual' ? 'case-row manual' : mode === 'form' ? 'case-row form' : 'case-row';
  const hasLoop = cases.some((c) => c.loop_to);

  return (
    <>
      <Field label="分支依据">
        <select value={mode} disabled={readOnly} onChange={(event) => set({ mode: event.target.value as typeof mode })}>
          <option value="manual">人工选择（操作员在批次页选出口）</option>
          <option value="measure">上游设备测量值（设备回执 delivered 里的键）</option>
          <option value="form">上游人工记录字段</option>
        </select>
      </Field>
      {mode !== 'manual' ? (
        <div className="grid cols-2">
          <Field label="判据来源（上游步骤）">
            <select value={config.source_step_id ?? ''} disabled={readOnly} onChange={(event) => set({ source_step_id: event.target.value })}>
              <option value="">选择步骤</option>
              {sources.map(({ row, at, id }) => (
                <option key={id} value={id}>
                  第 {at + 1} 步 · {row.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="判据字段">
            {mode === 'form' ? (
              <select value={config.field ?? ''} disabled={readOnly} onChange={(event) => set({ field: event.target.value })}>
                <option value="">选择字段</option>
                {(source?.form ?? []).map((field) => (
                  <option key={field.key} value={field.key}>
                    {field.label || field.key}
                  </option>
                ))}
              </select>
            ) : (
              <input value={config.field ?? ''} readOnly={readOnly} placeholder="如 mass、water_ppm" onChange={(event) => set({ field: event.target.value })} />
            )}
          </Field>
        </div>
      ) : null}
      <Field label="出口" hint="按顺序匹配，第一个满足条件的生效；回环出口会作废回环体后从目标重做">
        <div className="stack">
          <div className={`${rowClass} tiny muted`}>
            <span>标识</span>
            <span>名称</span>
            {mode === 'measure' ? (
              <>
                <span>下限</span>
                <span>上限</span>
              </>
            ) : null}
            {mode === 'form' ? <span>等于（或留空用上下限）</span> : null}
            <span>回到（可选）</span>
            <span />
          </div>
          {cases.map((row, position) => (
            <div key={position} className={rowClass}>
              <input value={row.key} readOnly={readOnly} onChange={(event) => setCase(position, { key: event.target.value.trim() })} />
              <input value={row.label} readOnly={readOnly} onChange={(event) => setCase(position, { label: event.target.value })} />
              {mode === 'measure' ? (
                <>
                  <NumberInput value={row.min ?? ''} disabled={readOnly} ariaLabel="下限" onChange={(next) => setCase(position, { min: numberOrNull(next) })} />
                  <NumberInput value={row.max ?? ''} disabled={readOnly} ariaLabel="上限" onChange={(next) => setCase(position, { max: numberOrNull(next) })} />
                </>
              ) : null}
              {mode === 'form' ? (
                <input value={row.equals ?? ''} readOnly={readOnly} onChange={(event) => setCase(position, { equals: event.target.value })} />
              ) : null}
              <select value={row.loop_to ?? ''} disabled={readOnly} onChange={(event) => setCase(position, { loop_to: event.target.value || undefined })}>
                <option value="">往下走</option>
                {earlier.map(({ row: target, at, id }) => (
                  <option key={id} value={id}>
                    回到第 {at + 1} 步 · {target.name}
                  </option>
                ))}
              </select>
              <button
                className="btn sm"
                disabled={readOnly || cases.length <= 2}
                title={cases.length <= 2 ? '至少保留两个出口' : '删除出口'}
                onClick={() => onSet((current) => void (current.branch = { ...current.branch, cases: branchCases(current).filter((_, at) => at !== position) }))}
              >
                ×
              </button>
            </div>
          ))}
          <button
            className="btn sm"
            disabled={readOnly}
            onClick={() =>
              onSet((current) => {
                const rows = branchCases(current);
                current.branch = { ...current.branch, cases: [...rows, { key: `c${rows.length + 1}`, label: `出口 ${rows.length + 1}` }] };
              })
            }
          >
            增加出口
          </button>
        </div>
      </Field>
      <div className="grid cols-2">
        {mode !== 'manual' ? (
          <Field label="默认出口" hint="判据有值但不满足任何条件时走它；判据缺失时一律转人工">
            <select value={config.default ?? ''} disabled={readOnly} onChange={(event) => set({ default: event.target.value || undefined })}>
              <option value="">无（保持待人工选择）</option>
              {forwardCaseKeys(step).map((key) => (
                <option key={key} value={key}>
                  {cases.find((c) => c.key === key)?.label ?? key}
                </option>
              ))}
            </select>
          </Field>
        ) : null}
        {hasLoop ? (
          <Field label="最多循环次数" hint="超过后转人工选择出口">
            <NumberInput
              value={config.max_loops ?? ''}
              disabled={readOnly}
              ariaLabel="最多循环次数"
              onChange={(next) => set({ max_loops: next === '' ? undefined : next })}
            />
          </Field>
        ) : null}
      </div>
      <label className="check">
        <input
          type="checkbox"
          checked={Boolean(step.requires_signature)}
          disabled={readOnly}
          onChange={(event) => onSet((current) => void (current.requires_signature = event.target.checked))}
        />
        人工选择出口时要求电子签名
      </label>
    </>
  );
}

/* 子流程：引用一个已发布的方法，建批次时展开进快照。被引用方法修订发布后旧版本退役，引用随之失效，
   要改这里重新评审——不会悄悄换掉已批准流程里的一段。 */
function SubflowFields({
  step,
  recipeId,
  recipes,
  subflows,
  readOnly,
  onSet,
}: {
  step: RecipeStep;
  recipeId: string;
  recipes: RecipeSummary[];
  subflows: SubflowIndex;
  readOnly: boolean;
  onSet: (change: (step: RecipeStep) => void) => void;
}) {
  const chosen = step.subflow?.recipe_id ?? '';
  const target = subflows[chosen];
  const options = recipes.filter((row) => row.id !== recipeId && row.state === 'released' && !row.needs_revision);
  return (
    <>
      <Field label="引用的方法" hint="只列已发布、无需修订的方法；子方法的 BOM 会并入批次物料预留">
        <select
          value={chosen}
          disabled={readOnly}
          onChange={(event) =>
            onSet((current) => {
              const recipe = recipes.find((row) => row.id === event.target.value);
              const wasDefault = !current.name || current.name === '子流程' || recipes.some((row) => row.name === current.name);
              current.subflow = { recipe_id: event.target.value };
              if (recipe && wasDefault) current.name = recipe.name;
            })
          }
        >
          <option value="">选择方法</option>
          {options.map((row) => (
            <option key={row.id} value={row.id}>
              {row.id} · {row.name} v{row.version}（{row.step_count} 步，关键路径 {row.critical_path_min} min）
            </option>
          ))}
          {chosen && !options.some((row) => row.id === chosen) ? (
            <option value={chosen}>{chosen}（已失效）</option>
          ) : null}
        </select>
      </Field>
      {target ? (
        <div className="small muted">
          {target.name} v{target.version} · {target.state === 'released' && !target.needs_revision ? '有效' : '已失效，请改引用新版本'} ·{' '}
          <Link to={`/recipes/${chosen}`}>查看</Link>
        </div>
      ) : null}
    </>
  );
}

function TimeoutFields({
  step,
  actions,
  readOnly,
  onSet,
}: {
  step: RecipeStep;
  actions: string[];
  readOnly: boolean;
  onSet: (change: (step: RecipeStep) => void) => void;
}) {
  const timeout = step.timeout;
  const fixedWait = kindOf(step) === 'wait' && (step.wait_for?.mode ?? 'duration') === 'duration';
  if (fixedWait) return null;
  return (
    <>
      <label className="check">
        <input
          type="checkbox"
          checked={!!timeout}
          disabled={readOnly}
          onChange={(event) =>
            onSet((current) => {
              if (event.target.checked) current.timeout = { minutes: Math.max(5, Number(current.dur) * 2 || 30), action: 'alarm' };
              else delete current.timeout;
            })
          }
        />
        步骤级超时：开出后超过限定时长仍未完成时处理
      </label>
      {timeout ? (
        <div className="grid cols-2">
          <Field label="超时 min">
            <NumberInput
              value={timeout.minutes}
              invalid={!(timeout.minutes > 0)}
              disabled={readOnly}
              ariaLabel="超时分钟"
              onChange={(next) => onSet((current) => void (current.timeout = { ...current.timeout!, minutes: next === '' ? 0 : next }))}
            />
          </Field>
          <Field label="超时处理" hint={kindOf(step) === 'device' ? '设备步骤只报警：指令超时另有硬上限与人工核查' : undefined}>
            <select
              value={timeout.action}
              disabled={readOnly}
              onChange={(event) =>
                onSet((current) => void (current.timeout = { ...current.timeout!, action: event.target.value as 'alarm' | 'fail' | 'skip' }))
              }
            >
              {TIMEOUT_ACTIONS.filter(([value]) => actions.includes(value)).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
        </div>
      ) : null}
    </>
  );
}

/** 设备方法：流程管做什么，方法管怎么做。选一条已发布的方法后，能力随方法、参数取方法缺省值，
    只有方法适用型号、且设备报告支持该程序的工位才能承接。 */
function MethodField({
  step,
  methods,
  readOnly,
  onSet,
}: {
  step: RecipeStep;
  methods: DeviceMethodRow[];
  readOnly: boolean;
  onSet: (change: (step: RecipeStep) => void) => void;
}) {
  const candidates = methods.filter((row) => !step.cap || row.capability_id === step.cap);
  const current = step.method?.id;
  const known = current ? methods.find((row) => row.id === current) : undefined;
  return (
    <Field
      label="设备方法"
      hint={
        current
          ? known
            ? `${known.code} v${known.version} · 程序 ${known.program || '—'} · 适用型号 ${known.instrument_models.join('、') || '不限'}`
            : `引用的方法 ${step.method?.code ?? current} v${step.method?.version ?? '?'} 已不是有效发布版本，请改选`
          : '不引用时参数直接写在步骤上；引用后参数取方法缺省值并受方法范围约束'
      }
    >
      <select
        value={current ?? ''}
        disabled={readOnly}
        onChange={(event) =>
          onSet((draft) => {
            const picked = methods.find((row) => row.id === event.target.value);
            if (!picked) {
              delete draft.method;
              return;
            }
            draft.method = {
              id: picked.id, code: picked.code, version: picked.version, name: picked.name, program: picked.program,
              instrument_models: picked.instrument_models, params: picked.params,
            };
            draft.cap = picked.capability_id;
            const defaults = Object.fromEntries(
              Object.entries(picked.params)
                .filter(([, rule]) => rule.default != null)
                .map(([key, rule]) => [key, Number(rule.default)]),
            );
            draft.params = { ...draft.params, ...defaults };
            if (picked.dur_min) draft.dur = picked.dur_min;
          })
        }
      >
        <option value="">不引用设备方法</option>
        {current && !known ? <option value={current}>{`${step.method?.code ?? current} v${step.method?.version ?? '?'}（失效）`}</option> : null}
        {candidates.map((row) => (
          <option key={row.id} value={row.id}>
            {row.code} v{row.version} · {row.name}
          </option>
        ))}
      </select>
    </Field>
  );
}
