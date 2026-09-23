/* 配方图形化编辑：能力面板 → 流程画布 → 属性面板。

   三条设计约束，与原型一致：
   1. 步骤绑定能力而不是设备，参数实时对照全部工位极限的并集；
   2. 硬时限画在进入该步的连线上，因为它约束的是「上一事件到本步开始」的间隔；
   3. 恢复规则由能力继承，配方不可覆盖，所以属性面板里只读展示。 */
import type { ReactNode } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api } from '../../shared/api';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type { BomItem, CapabilityRow, FormField, LotRow, RecipeDetail, RecipeStep, SopVersionRow, StationRow } from '../../shared/types';
import { CheckList, Field, NumberInput, Panel, Pill, useToast } from '../../shared/ui';
import {
  canSubmit,
  defaultParams,
  editorChecks,
  indexCapabilities,
  paramRange,
  stationsForStep,
  stepIssues,
  kindOf,
  needsStation,
  STEP_KINDS,
  type StepKind,
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

export function RecipeEditorPage() {
  const { recipeId = '' } = useParams();
  const navigate = useNavigate();
  const { can } = useSession();
  const toast = useToast();

  const recipe = useQuery<RecipeDetail>(`recipes:${recipeId}`, () => api.get<RecipeDetail>(`/recipes/${recipeId}`));
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const stations = useQuery<StationRow[]>('stations', () => api.get<StationRow[]>('/stations'));
  const lots = useQuery<LotRow[]>('lots', () => api.get<LotRow[]>('/lots'));
  // 只取已发布且生效的版本：草稿与已退役的 SOP 不该被新方法引用
  const sops = useQuery<SopVersionRow[]>('sops:effective', () => api.get<SopVersionRow[]>('/sops/effective'));

  const [draft, setDraft] = useState<Draft | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
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
    setSelected(data.steps?.length ? 0 : null);
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
          )
        : [],
    [draft, stations.data, capabilityIndex, sops.data],
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

  /* ---------- 编辑动作 ---------- */

  const mutate = (change: (current: Draft) => Draft) => {
    setDraft((current) => (current ? change(clone(current)) : current));
    setDirty(true);
  };

  const insert = (step: RecipeStep) => {
    const at = selected === null ? draft.steps.length : selected + 1;
    mutate((current) => {
      current.steps.splice(at, 0, step);
      return current;
    });
    setSelected(at);
  };

  const addStep = (capability: CapabilityRow) =>
    insert({
      kind: 'device',
      name: capability.name,
      cap: capability.id,
      params: defaultParams(stations.data, capability),
      dur: 15,
    });

  /** 人工、等待、审核节点不绑定能力，也不占工位——它们不需要「可承接工位」。 */
  const addNonDeviceStep = (kind: 'manual' | 'wait' | 'review') => {
    if (kind === 'manual') {
      insert({
        kind: 'manual',
        name: '人工步骤',
        cap: '',
        params: {},
        dur: 15,
        requires_sample_check: true,
        form: [{ key: 'value', label: '记录值', type: 'number', required: true }],
      });
      return;
    }
    if (kind === 'wait') {
      insert({ kind: 'wait', name: '等待', cap: '', params: {}, dur: 30, wait_for: { mode: 'duration' } });
      return;
    }
    insert({ kind: 'review', name: '审核', cap: '', params: {}, dur: 0, review_role: 'qa' });
  };

  const setStep = (index: number, change: (step: RecipeStep) => void) =>
    mutate((current) => {
      change(current.steps[index]);
      return current;
    });

  const moveStep = (from: number, to: number) => {
    if (to < 0 || to >= draft.steps.length || from === to) return;
    mutate((current) => {
      const [step] = current.steps.splice(from, 1);
      current.steps.splice(to, 0, step);
      return current;
    });
    setSelected(to);
  };

  const duplicateStep = (index: number) => {
    mutate((current) => {
      current.steps.splice(index + 1, 0, clone(current.steps[index]));
      return current;
    });
    setSelected(index + 1);
  };

  const deleteStep = (index: number) => {
    mutate((current) => {
      current.steps.splice(index, 1);
      return current;
    });
    setSelected(draft.steps.length > 1 ? Math.min(index, draft.steps.length - 2) : null);
  };

  const changeCapability = (index: number, capabilityId: string) => {
    const capability = capabilityIndex[capabilityId];
    if (!capability) return;
    setStep(index, (step) => {
      const wasDefaultName = step.name === capabilityIndex[step.cap]?.name;
      step.cap = capabilityId;
      step.params = defaultParams(stations.data, capability);
      if (wasDefaultName) step.name = capability.name;
    });
  };

  const toggleHard = (index: number, on: boolean) =>
    setStep(index, (step) => {
      if (on) {
        const previous = draft.steps[index - 1];
        step.hard = { from: previous ? `${previous.name}结束` : '托盘就位', maxGapMin: 30 };
      } else {
        delete step.hard;
      }
    });

  const discard = () => {
    setDraft({
      steps: clone(data.steps ?? []),
      bom: clone(data.bom ?? []),
      meta: {
        name: data.name, plate: data.plate, design: data.design, risk: data.risk,
        sop_version_id: data.sop_version_id ?? '',
      },
    });
    setSelected(data.steps?.length ? 0 : null);
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

  /* ---------- 画布 ---------- */

  const canvas = (
    <div className="fcanvas">
      <div className="fnode term">
        开始
        <div className="small muted">托盘就位</div>
      </div>
      {draft.steps.map((step, index) => (
        <FlowNode
          key={index}
          index={index}
          step={step}
          selected={selected === index}
          readOnly={readOnly}
          issues={stepIssues(step, capabilityIndex)}
          fits={stationsForStep(stations.data, step).map((station) => station.id)}
          capabilityName={capabilityIndex[step.cap]?.name ?? step.cap}
          paramLabels={capabilityIndex[step.cap]?.params ?? {}}
          onSelect={() => setSelected(index)}
          onDrop={(from) => moveStep(from, index)}
        />
      ))}
      <Edge />
      <DropTarget disabled={readOnly} onDrop={(from) => moveStep(from, draft.steps.length - 1)}>
        <div className="fnode term">
          结束
          <div className="small muted">样品交付检测</div>
        </div>
      </DropTarget>
    </div>
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
          支持四类顺序节点：设备、人工、等待、审核。设备步骤绑定能力而不是设备，参数实时对照全部工位极限；
          人工步骤定义结构化记录表单；等待当前只支持固定时长；审核批准才继续。
          这里的校验是即时提示，能不能提交由服务端重算。
        </div>
      )}

      <div className="designer">
        <Panel title="节点面板">
          <div className="palette">
            <div className="cap">
              <div className="cap-head">
                <b>非设备节点</b>
              </div>
              <div className="small muted">人工记录、定时等待、流程审核；它们不绑定能力，也不占工位。</div>
              <div className="filters" style={{ marginTop: 6 }}>
                <button className="btn sm" disabled={readOnly} onClick={() => addNonDeviceStep('manual')}>
                  + 人工
                </button>
                <button className="btn sm" disabled={readOnly} onClick={() => addNonDeviceStep('wait')}>
                  + 等待
                </button>
                <button className="btn sm" disabled={readOnly} onClick={() => addNonDeviceStep('review')}>
                  + 审核
                </button>
              </div>
            </div>
            {(capabilities.data ?? []).map((capability) => (
              <div key={capability.id} className="cap">
                <div className="cap-head">
                  <b>{capability.name}</b>
                  <button className="btn sm" disabled={readOnly} onClick={() => addStep(capability)}>
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
            title={`流程画布 · ${draft.steps.length} 步`}
            aside={<span className="small muted">拖动节点调整顺序</span>}
            flush
          >
            {canvas}
            {draft.steps.length ? null : (
              <div className="empty">
                从左侧面板添加步骤。设备步骤绑定能力而不是设备，参数对照全部工位的极限校验；
                人工、等待、审核节点不占工位。
              </div>
            )}
          </Panel>
          <Panel title="校验">
            <CheckList checks={checks} />
            <div className="small muted">前五项通过才能提交评审；风险评估编号可在发布前补齐。</div>
          </Panel>
        </div>

        {selected !== null && draft.steps[selected] ? (
          <StepProperties
            index={selected}
            step={draft.steps[selected]}
            previous={draft.steps[selected - 1]}
            capabilities={capabilities.data ?? []}
            capabilityIndex={capabilityIndex}
            stations={stations.data}
            readOnly={readOnly}
            total={draft.steps.length}
            onBack={() => setSelected(null)}
            onSet={(change) => setStep(selected, change)}
            onCapability={(id) => changeCapability(selected, id)}
            onHard={(on) => toggleHard(selected, on)}
            onMove={(to) => moveStep(selected, to)}
            onDuplicate={() => duplicateStep(selected)}
            onDelete={() => deleteStep(selected)}
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

/* ---------- 画布节点与连线 ---------- */

/* 拖拽用 HTML5 原生 DnD：节点是线性链，不需要自由布局，省一个依赖。 */
const DRAG_TYPE = 'application/x-ilcs-step';

function Edge({ hard }: { hard?: { from: string; maxGapMin: number } }) {
  return (
    <div className={`fedge${hard ? ' hard' : ''}`}>
      {hard ? (
        <span className="fedge-label" title={`自${hard.from}起 ${hard.maxGapMin} min 内必须开始`}>
          ≤{hard.maxGapMin} min
        </span>
      ) : null}
    </div>
  );
}

function DropTarget({
  children,
  disabled,
  onDrop,
}: {
  children: ReactNode;
  disabled?: boolean;
  onDrop: (from: number) => void;
}) {
  const [over, setOver] = useState(false);
  if (disabled) return <>{children}</>;
  return (
    <div
      className={`drop-end${over ? ' over' : ''}`}
      onDragOver={(event) => {
        event.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setOver(false);
        const from = Number(event.dataTransfer.getData(DRAG_TYPE));
        if (Number.isInteger(from)) onDrop(from);
      }}
    >
      {children}
    </div>
  );
}

function FlowNode({
  index,
  step,
  selected,
  readOnly,
  issues,
  fits,
  capabilityName,
  paramLabels,
  onSelect,
  onDrop,
}: {
  index: number;
  step: RecipeStep;
  selected: boolean;
  readOnly: boolean;
  issues: string[];
  fits: string[];
  capabilityName: string;
  paramLabels: Record<string, string>;
  onSelect: () => void;
  onDrop: (from: number) => void;
}) {
  const [over, setOver] = useState(false);
  const [dragging, setDragging] = useState(false);
  const kind = kindOf(step);
  const requiresStation = needsStation(step);
  // 不占工位的节点没有「可承接工位」这回事，缺工位不算问题
  const bad = issues.length > 0 || (requiresStation && fits.length === 0);
  const classes = ['fnode', selected ? 'sel' : '', bad ? 'bad' : '', over ? 'over' : '', dragging ? 'dragging' : ''];

  return (
    <>
      <Edge hard={step.hard} />
      <button
        type="button"
        className={classes.filter(Boolean).join(' ')}
        draggable={!readOnly}
        title={
          bad
            ? [...issues, ...(requiresStation && !fits.length ? ['没有工位能承接这些参数'] : [])].join('；')
            : '点击编辑属性，拖动调整顺序'
        }
        onClick={onSelect}
        onDragStart={(event) => {
          event.dataTransfer.effectAllowed = 'move';
          event.dataTransfer.setData(DRAG_TYPE, String(index));
          setDragging(true);
        }}
        onDragEnd={() => setDragging(false)}
        onDragOver={(event) => {
          if (readOnly) return;
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          const from = Number(event.dataTransfer.getData(DRAG_TYPE));
          if (Number.isInteger(from)) onDrop(from);
        }}
      >
        <span className="fn-head">
          <span className="mono">{index + 1}</span>
          <span className={`kind ${kind}`}>
            {STEP_KINDS.find(([value]) => value === kind)?.[1] ?? kind}
          </span>
          {kind === 'device' ? <span className="tag">{capabilityName}</span> : null}
        </span>
        <span className="fn-title">{step.name || <span className="muted">未命名</span>}</span>
        <span className="fn-meta">
          {kind === 'device'
            ? Object.entries(step.params ?? {})
                .map(([key, value]) => `${(paramLabels[key] ?? key).split(' ')[0]} ${value === '' ? '?' : value}`)
                .join(' · ') || '无参数'
            : kind === 'manual'
            ? `${(step.form ?? []).length} 个记录字段`
            : kind === 'wait'
            ? step.wait_for?.mode === 'event'
              ? `等待事件 ${step.wait_for.event || '未选择'}`
              : '定时等待'
            : `审核角色 ${step.review_role || 'qa'}`}
        </span>
        <span className="fn-foot">
          <span className="mono">{kind === 'review' ? '—' : `${step.dur} min`}</span>
          {requiresStation ? (
            <span className={fits.length ? 'muted' : 'bad'}>{fits.length ? fits.join(' ') : '无可承接工位'}</span>
          ) : (
            <span className="muted">不占工位</span>
          )}
        </span>
      </button>
    </>
  );
}

/* ---------- 属性面板 ---------- */

function StepProperties({
  index,
  step,
  previous,
  capabilities,
  capabilityIndex,
  stations,
  readOnly,
  total,
  onBack,
  onSet,
  onCapability,
  onHard,
  onMove,
  onDuplicate,
  onDelete,
}: {
  index: number;
  step: RecipeStep;
  previous?: RecipeStep;
  capabilities: CapabilityRow[];
  capabilityIndex: Record<string, CapabilityRow>;
  stations: StationRow[] | undefined;
  readOnly: boolean;
  total: number;
  onBack: () => void;
  onSet: (change: (step: RecipeStep) => void) => void;
  onCapability: (id: string) => void;
  onHard: (on: boolean) => void;
  onMove: (to: number) => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const capability = capabilityIndex[step.cap];
  const recovery = capability?.recovery ?? {};
  const fits = stationsForStep(stations, step);
  const kind = kindOf(step);

  return (
    <Panel title={`第 ${index + 1} 步`} aside={<button className="btn sm" onClick={onBack}>方法属性</button>}>
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
              if (next === 'manual' && !current.form?.length) {
                current.form = [{ key: 'value', label: '记录值', type: 'number', required: true }];
              }
              if (next === 'wait' && !current.wait_for) current.wait_for = { mode: 'duration' };
              if (next === 'review' && !current.review_role) current.review_role = 'qa';
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

      {kind === 'manual' ? (
        <>
          <Field label="记录表单" hint="人工步骤必须有结构化记录；缺必填项时服务端不推进">
            <div className="stack">
              {(step.form ?? []).map((field, position) => (
                <div className="filters" key={position}>
                  <input
                    placeholder="字段标识"
                    value={field.key}
                    readOnly={readOnly}
                    onChange={(event) =>
                      onSet((current) => void (current.form![position].key = event.target.value))
                    }
                  />
                  <input
                    placeholder="显示名称"
                    value={field.label}
                    readOnly={readOnly}
                    onChange={(event) =>
                      onSet((current) => void (current.form![position].label = event.target.value))
                    }
                  />
                  <select
                    value={field.type ?? 'text'}
                    disabled={readOnly}
                    onChange={(event) =>
                      onSet(
                        (current) =>
                          void (current.form![position].type = event.target.value as FormField['type']),
                      )
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
                      onChange={(event) =>
                        onSet((current) => void (current.form![position].required = event.target.checked))
                      }
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
                    current.form = [
                      ...(current.form ?? []),
                      { key: '', label: '', type: 'text', required: true },
                    ];
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
                onChange={(event) =>
                  onSet((current) => void (current.requires_signature = event.target.checked))
                }
              />
              提交需要电子签名
            </label>
            <label className="small">
              <input
                type="checkbox"
                checked={Boolean(step.consumes_materials)}
                disabled={readOnly}
                onChange={(event) =>
                  onSet((current) => void (current.consumes_materials = event.target.checked))
                }
              />
              该步骤消耗 BOM 物料（勾了才要求 BOM 与投料许可）
            </label>
          </Field>
        </>
      ) : null}

      {kind === 'wait' ? (
        <Field label="等待方式" hint="当前只支持固定时长；业务事件等待在事件接口落地前不可用">
          <select
            value={step.wait_for?.mode ?? 'duration'}
            disabled={readOnly}
            onChange={(event) =>
              onSet(
                (current) =>
                  void (current.wait_for = { ...current.wait_for, mode: event.target.value as 'duration' | 'event' }),
              )
            }
          >
            <option value="duration">固定时长</option>
            <option value="event" disabled>
              业务事件（暂不支持）
            </option>
          </select>
          {step.wait_for?.mode === 'event' ? (
            <div className="note warn">
              该步骤仍是旧的「业务事件」等待（{step.wait_for?.event || '未选择事件'}），开跑后不会被唤醒，请改为固定时长。
            </div>
          ) : null}
        </Field>
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
        const ok = !!range && typeof value === 'number' && value >= range[0] && value <= range[1];
        return (
          <Field
            key={key}
            label={`${paramLabel}　${range ? `[${range[0]}, ${range[1]}]` : '无工位定义该参数'}`}
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

      <Field label="计划时长 min" hint="仅用于排程；实际终点由设备事件决定">
        <NumberInput
          value={step.dur}
          invalid={!(step.dur > 0)}
          disabled={readOnly}
          ariaLabel="计划时长"
          onChange={(next) => onSet((current) => void (current.dur = next === '' ? 0 : next))}
        />
      </Field>

      <label className="check">
        <input
          type="checkbox"
          checked={!!step.hard}
          disabled={readOnly}
          onChange={(event) => onHard(event.target.checked)}
        />
        硬时限：本步必须在上一事件后的限定时间内开始
      </label>

      {step.hard ? (
        <div className="grid cols-2">
          <Field label="起算事件">
            <input
              value={step.hard.from}
              readOnly={readOnly}
              placeholder={previous ? `${previous.name}结束` : '托盘就位'}
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

      <div className="small muted">
        恢复规则（继承自能力，配方不可覆盖）：
        {recovery.pausable ? `可保持 ≤ ${recovery.maxHoldMin} min，${recovery.hold}` : '不可保持'} ·{' '}
        {recovery.retryable ? '可重试' : '不可重试'}
        {recovery.verify?.length ? ` · 恢复前核实 ${recovery.verify.join('、')}` : ''}
      </div>

      <div className="actions">
        <button className="btn sm" disabled={readOnly || index === 0} onClick={() => onMove(index - 1)}>
          ◀ 前移
        </button>
        <button className="btn sm" disabled={readOnly || index === total - 1} onClick={() => onMove(index + 1)}>
          后移 ▶
        </button>
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
