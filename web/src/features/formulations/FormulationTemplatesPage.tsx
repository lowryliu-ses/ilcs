/* 配液模板：一条配液线「怎么把一张配方表变成流程和方案」的规则——固定的前段与后段、加料阶段、物料类别怎么加、
   加完做什么、每次实验可改的参数、表格里逐瓶给的参数、分装量核对。在这里可视化编辑，不用手写 JSON。

   模板不走发布：它只决定怎么生成流程草稿，生成的流程照旧评审 → 批准 → 发布，那才是受控点。改模板带 row_version、
   留审计；不用的退役。编辑时每改一处就按当前主数据（能力、已发布的设备方法、在用指标、生效 SOP）核一次，
   也可以上传一张配方表按还没保存的配置试算——与导入同一套规则，不写库、不登记瓶子。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';

import { api } from '../../shared/api';
import { clock } from '../../shared/format';
import { paramSpec } from '../../shared/params';
import { ProgramTableEditor } from '../../shared/program';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import type {
  CapabilityRow, DeviceMethodRow, FormField, FormulationCheck, FormulationExperimentParam, FormulationFixedStep,
  FormulationRoute, FormulationRowParam, FormulationStage, FormulationTemplate, FormulationTemplateConfig,
  MaterialRow, MetricRow, ProgramRow, RecipeStep, SopVersionRow,
} from '../../shared/types';
import { Blocked, ConfirmDialog, Empty, Field, ListState, NumberInput, Panel, Pill, useToast } from '../../shared/ui';
import { withUnit } from '../../shared/units';

const BASE = '/formulation-templates';
const DEFAULT_SERIAL_HEADERS = ['序列号', '编号', '瓶号', '样品编号', 'serial', 'id'];

/* ---------- 列表 ---------- */

export function FormulationTemplatesPage() {
  const { can } = useSession();
  const toast = useToast();
  const templates = useQuery<FormulationTemplate[]>('formulation-templates', () => api.get<FormulationTemplate[]>(BASE));
  const [retiring, setRetiring] = useState<FormulationTemplate | null>(null);
  const retire = useMutation(
    (row: FormulationTemplate) => api.post(`${BASE}/${row.id}/retire`, { row_version: row.row_version }),
    {
      invalidates: ['formulation-templates', 'audit'],
      onSuccess: () => {
        toast.push('模板已退役：配方导入不再能选它，已生成的流程与方案不受影响');
        setRetiring(null);
      },
    },
  );
  const editable = can('recipe.edit');

  return (
    <div className="page">
      <div className="page-head">
        <h1>配液模板</h1>
        <span className="small muted">
          一条配液线怎么把一张配方表变成流程和方案：固定前后段、加料阶段、物料类别的加法、加完做什么、每次可改的参数。
          生成的流程照旧评审、批准、发布。
        </span>
        <Link className="btn" to="/formulations">
          去配方导入
        </Link>
        {editable ? (
          <Link className="btn primary" to={`${BASE}/new`}>
            新建模板
          </Link>
        ) : null}
      </div>
      <Panel title={`模板（${templates.data?.length ?? 0}）`} flush>
        <ListState loading={templates.loading && !templates.data} error={templates.error} />
        {templates.data && !templates.data.length ? <Empty>还没有配液模板</Empty> : null}
        {templates.data?.length ? (
          <table>
            <thead>
              <tr>
                <th>编号</th>
                <th>名称</th>
                <th>状态</th>
                <th>更新</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {templates.data.map((row) => (
                <tr key={row.id} className={row.state === 'active' ? undefined : 'retired-row'}>
                  <td className="mono">{row.code}</td>
                  <td>
                    {row.name}
                    {row.description ? <div className="tiny muted">{row.description}</div> : null}
                    <div className="tiny muted">
                      {(row.config?.stages ?? []).length} 个加料阶段 · {Object.keys(row.config?.routes ?? {}).length} 类物料 ·{' '}
                      {(row.config?.prefix?.length ?? 0) + (row.config?.suffix?.length ?? 0)} 个固定前后段步骤
                    </div>
                  </td>
                  <td>
                    <Pill state={row.state === 'active' ? 'running' : 'retired'} label={row.state_label ?? row.state} />
                  </td>
                  <td className="small">{row.updated_at ? clock(row.updated_at) : '—'}</td>
                  <td className="row-end">
                    <Link className="btn sm" to={`${BASE}/${row.id}`}>
                      {editable && row.state === 'active' ? '编辑' : '查看'}
                    </Link>
                    {editable ? (
                      <Link className="btn sm" to={`${BASE}/new?copy=${row.id}`} title="以这份模板为起点另建一份">
                        复制为新模板
                      </Link>
                    ) : null}
                    {editable && row.state === 'active' ? (
                      <button className="btn sm danger" onClick={() => setRetiring(row)}>
                        退役
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>
      {retiring ? (
        <ConfirmDialog
          title={`退役配液模板 · ${retiring.code}`}
          danger
          confirmLabel="退役"
          pending={retire.pending}
          error={retire.error?.message}
          onClose={() => setRetiring(null)}
          onConfirm={() => retire.run(retiring).catch(() => undefined)}
        >
          <div className="note warn">退役后配方导入不再能选它；已经生成的流程与方案不受影响。退役不删除，导入审计仍指回它。</div>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

/* ---------- 编辑 ---------- */

type Draft = { code: string; name: string; description: string; config: FormulationTemplateConfig };

function blankConfig(): FormulationTemplateConfig {
  return {
    plate: 24, risk: '', unit: 'g', sample_type: '', design: '', serial_headers: [...DEFAULT_SERIAL_HEADERS],
    required_metrics: [], prefix: [],
    stages: [{ key: 'main', label: '加料', after: [], stir_after_last: true, then: [] }],
    routes: {}, suffix: [], experiment_params: [], row_params: [],
  };
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** 编辑时要用到的主数据 */
type Lookups = {
  capabilities: CapabilityRow[];
  methods: DeviceMethodRow[];
  sopTitles: string[];
};

export function FormulationTemplateEditorPage() {
  const { templateId = 'new' } = useParams();
  const [search] = useSearchParams();
  const copyFrom = search.get('copy') ?? '';
  const creating = templateId === 'new';
  const sourceId = creating ? copyFrom : templateId;
  const navigate = useNavigate();
  const toast = useToast();
  const { can } = useSession();

  const source = useQuery<FormulationTemplate>(sourceId ? `formulation-templates:${sourceId}` : null, () =>
    api.get<FormulationTemplate>(`${BASE}/${sourceId}`),
  );
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const methods = useQuery<DeviceMethodRow[]>('device-methods:released', () =>
    api.get<DeviceMethodRow[]>('/device-methods?state=released'),
  );
  const metrics = useQuery<MetricRow[]>('metrics', () => api.get<MetricRow[]>('/metrics'));
  const sops = useQuery<SopVersionRow[]>('sops:effective', () => api.get<SopVersionRow[]>('/sops/effective'));
  const materials = useQuery<MaterialRow[]>('materials', () => api.get<MaterialRow[]>('/materials'));

  const [draft, setDraft] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);
  const loaded = useRef('');
  useEffect(() => {
    const key = `${templateId}|${copyFrom}`;
    if (loaded.current === key) return;
    if (sourceId && !source.data) return;
    loaded.current = key;
    const base = source.data;
    setDraft(
      base && !creating
        ? { code: base.code, name: base.name, description: base.description, config: clone(base.config ?? blankConfig()) }
        : base
        ? { code: `${base.code}-COPY`, name: `${base.name}（副本）`, description: base.description, config: clone(base.config ?? blankConfig()) }
        : { code: '', name: '', description: '', config: blankConfig() },
    );
    setDirty(false);
  }, [templateId, copyFrom, sourceId, source.data, creating]);

  const readOnly = !can('recipe.edit') || (!creating && source.data?.state !== 'active');
  const mutate = (change: (config: FormulationTemplateConfig) => void) => {
    setDraft((current) => {
      if (!current) return current;
      const config = clone(current.config);
      change(config);
      return { ...current, config };
    });
    setDirty(true);
  };

  /* 每改一处，按当前主数据核一次（停手 0.6 s 后发） */
  const [check, setCheck] = useState<FormulationCheck | null>(null);
  const configKey = draft ? JSON.stringify(draft.config) : '';
  useEffect(() => {
    if (!draft) return undefined;
    const timer = window.setTimeout(() => {
      api.post<FormulationCheck>(`${BASE}/check`, { config: draft.config }).then(setCheck).catch(() => undefined);
    }, 600);
    return () => window.clearTimeout(timer);
  }, [configKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation(
    () =>
      creating
        ? api.post<FormulationTemplate>(BASE, {
            code: draft!.code.trim(), name: draft!.name.trim(), description: draft!.description, config: draft!.config,
          })
        : api.patch<FormulationTemplate>(`${BASE}/${templateId}`, {
            name: draft!.name.trim(), description: draft!.description, config: draft!.config,
            row_version: source.data?.row_version,
          }),
    {
      invalidates: ['formulation-templates', 'audit'],
      onSuccess: (row) => {
        toast.push(creating ? '模板已建好，配方导入里可以选它了' : '模板已保存；之后导入按新规则生成');
        setDirty(false);
        loaded.current = '';
        navigate(`${BASE}/${row.id}`, { replace: true });
      },
    },
  );

  const lookups: Lookups = useMemo(() => {
    const code = draft?.config.sop?.code;
    const sop = (sops.data ?? []).find((row) => row.code === code);
    return {
      capabilities: capabilities.data ?? [],
      methods: methods.data ?? [],
      sopTitles: (sop?.steps ?? []).map((row) => row.title),
    };
  }, [capabilities.data, methods.data, sops.data, draft?.config.sop?.code]);

  if (!draft) {
    return (
      <div className="page">
        <ListState loading error={source.error} />
      </div>
    );
  }
  const config = draft.config;
  const fixed = fixedSteps(config);
  const problems = check?.problems ?? [];
  const serverProblems = (save.error?.payload as { detail?: { problems?: string[] } } | undefined)?.detail?.problems ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>{creating ? '新建配液模板' : `配液模板 · ${source.data?.code ?? ''}`}</h1>
        {!creating && source.data ? <Pill state={source.data.state === 'active' ? 'running' : 'retired'} label={source.data.state_label ?? ''} /> : null}
        <span className="small muted">{dirty ? '有未保存的改动' : readOnly ? '只读' : '改完点保存；右边实时列出问题'}</span>
        <Link className="btn" to={BASE}>
          返回列表
        </Link>
        {readOnly ? null : (
          <button
            className="btn primary"
            disabled={save.pending || !draft.name.trim() || (creating && !draft.code.trim()) || problems.length > 0}
            title={problems.length ? '先解决右边列出的问题' : undefined}
            onClick={() => save.run().catch(() => undefined)}
          >
            {creating ? '建立模板' : '保存'}
          </button>
        )}
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 1fr)', alignItems: 'start', gap: 16 }}>
        <div className="grid" style={{ gap: 16, minWidth: 0 }}>
          <Panel title="基本信息">
            <div className="grid cols-3">
              <Field label="编号" hint={creating ? '2–64 位，字母数字开头，如 FT-ELY-02' : '建好后不可更改'}>
                <input
                  className="mono"
                  value={draft.code}
                  disabled={!creating || readOnly}
                  onChange={(event) => {
                    setDraft({ ...draft, code: event.target.value });
                    setDirty(true);
                  }}
                />
              </Field>
              <Field label="名称">
                <input
                  value={draft.name}
                  disabled={readOnly}
                  onChange={(event) => {
                    setDraft({ ...draft, name: event.target.value });
                    setDirty(true);
                  }}
                />
              </Field>
              <Field label="每批样品位" hint="1–96：一批最多几瓶">
                <NumberInput value={config.plate} disabled={readOnly} ariaLabel="每批样品位"
                  onChange={(next) => mutate((c) => void (c.plate = next === '' ? 0 : next))} />
              </Field>
            </div>
            <Field label="说明">
              <textarea rows={2} value={draft.description} disabled={readOnly}
                onChange={(event) => {
                  setDraft({ ...draft, description: event.target.value });
                  setDirty(true);
                }} />
            </Field>
            <div className="grid cols-3">
              <Field label="风险评估编号" hint="开跑检查要求流程有风险评估编号">
                <input value={config.risk ?? ''} disabled={readOnly} onChange={(event) => mutate((c) => void (c.risk = event.target.value))} />
              </Field>
              <Field label="表头没写单位时按" hint="如 g">
                <input className="mono" value={config.unit ?? ''} disabled={readOnly} onChange={(event) => mutate((c) => void (c.unit = event.target.value))} />
              </Field>
              <Field label="样本类型">
                <input value={config.sample_type ?? ''} disabled={readOnly} onChange={(event) => mutate((c) => void (c.sample_type = event.target.value))} />
              </Field>
            </div>
            <Field label="设计说明" hint="写进生成的流程草稿">
              <input value={config.design ?? ''} disabled={readOnly} onChange={(event) => mutate((c) => void (c.design = event.target.value))} />
            </Field>
            <div className="grid cols-2">
              <Field label="序列号列的表头" hint="逗号或顿号分隔；不区分大小写">
                <input
                  value={(config.serial_headers ?? []).join('、')}
                  disabled={readOnly}
                  onChange={(event) =>
                    mutate((c) => void (c.serial_headers = event.target.value.split(/[、,，]+/).map((item) => item.trim()).filter(Boolean)))
                  }
                />
              </Field>
              <Field label="关联 SOP" hint="生成的流程按它当时生效的版本执行；步骤可选这份 SOP 里的步骤">
                <select
                  value={config.sop?.code ?? ''}
                  disabled={readOnly}
                  onChange={(event) =>
                    mutate((c) => {
                      if (event.target.value) c.sop = { code: event.target.value };
                      else delete c.sop;
                    })
                  }
                >
                  <option value="">不关联</option>
                  {[...new Set((sops.data ?? []).map((row) => row.code))].map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Field label="必测指标" hint="方案锁定要求至少一个；导入生成的方案带上它们">
              <div className="dep-list">
                {(metrics.data ?? [])
                  .filter((row) => row.state === 'active' || (config.required_metrics ?? []).includes(row.id))
                  .map((row) => (
                    <label key={row.id} className="check">
                      <input
                        type="checkbox"
                        disabled={readOnly}
                        checked={(config.required_metrics ?? []).includes(row.id)}
                        onChange={(event) =>
                          mutate((c) => {
                            const next = new Set(c.required_metrics ?? []);
                            if (event.target.checked) next.add(row.id);
                            else next.delete(row.id);
                            c.required_metrics = [...next];
                          })
                        }
                      />
                      {row.name} <span className="tiny muted mono">{row.code}</span>
                    </label>
                  ))}
              </div>
            </Field>
          </Panel>

          <Panel title="加料前的固定步骤">
            <StepList
              steps={config.prefix ?? []}
              earlier={[]}
              lookups={lookups}
              readOnly={readOnly}
              used={fixed.map((row) => row.key)}
              onChange={(steps) => mutate((c) => void (c.prefix = steps))}
            />
          </Panel>

          <ModePanel config={config} fixed={fixed} lookups={lookups} readOnly={readOnly} mutate={mutate} />
          <StagesPanel config={config} lookups={lookups} readOnly={readOnly} mutate={mutate} />
          <RoutesPanel config={config} lookups={lookups} readOnly={readOnly} mutate={mutate}
            categories={[...new Set((materials.data ?? []).filter((row) => row.state === 'active').map((row) => row.category).filter(Boolean))]} />

          {config.task ? null : (
          <Panel title="全局的加料后步骤">
            <div className="small muted">
              每加一种料之后做什么（通常是搅拌）。类别或阶段写了自己的加料后步骤就用它们的；名称里的 {'{material}'} 换成物料名。
              生成时按瓶执行：某瓶这种料是 0，这瓶跳过加料和随后的这一步。
            </div>
            <OptionalStep
              step={config.stir}
              label="全局加料后步骤"
              lookups={lookups}
              readOnly={readOnly}
              onChange={(step) =>
                mutate((c) => {
                  if (step) c.stir = step;
                  else delete c.stir;
                })
              }
            />
          </Panel>
          )}

          <Panel title="加料后的固定步骤">
            <StepList
              steps={config.suffix ?? []}
              earlier={fixed.filter((row) => row.section !== 'suffix')}
              lookups={lookups}
              readOnly={readOnly}
              used={fixed.map((row) => row.key)}
              onChange={(steps) => mutate((c) => void (c.suffix = steps))}
            />
          </Panel>

          <ParamsPanel config={config} fixed={fixed} lookups={lookups} readOnly={readOnly} mutate={mutate}
            specs={check?.param_specs ?? source.data?.param_specs ?? {}} />
          <VolumeCheckPanel config={config} readOnly={readOnly} mutate={mutate} />
        </div>

        <div className="grid" style={{ gap: 16, position: 'sticky', top: 12, minWidth: 0 }}>
          <Panel title={problems.length ? `问题（${problems.length}）` : '检查'}>
            {check ? (
              problems.length ? (
                <Blocked reasons={problems} />
              ) : (
                <div className="small">按当前主数据核对通过：能力、已发布的设备方法、在用指标、SOP 步骤都对得上。</div>
              )
            ) : (
              <div className="small muted">正在核对…</div>
            )}
            {save.error ? (
              <div className="note bad">
                保存失败：{save.error.message}
                <Blocked reasons={serverProblems} />
              </div>
            ) : null}
          </Panel>
          <TrialPanel draft={draft} />
        </div>
      </div>
    </div>
  );
}

/* ---------- 固定步骤 ---------- */

type FixedRef = { key: string; name: string; section: 'prefix' | 'stage' | 'suffix'; kind: string; cap: string };

/** 模板里全部固定步骤（按生成顺序），给「接在哪些步骤之后」「实验参数作用于哪一步」用 */
function fixedSteps(config: FormulationTemplateConfig): FixedRef[] {
  const pick = (steps: FormulationFixedStep[] | undefined, section: FixedRef['section']) =>
    (steps ?? []).map((row) => ({ key: row.key, name: row.name, section, kind: row.kind ?? 'device', cap: row.cap }));
  return [
    ...pick(config.prefix, 'prefix'),
    ...(config.stages ?? []).flatMap((stage) => pick(stage.then, 'stage')),
    ...pick(config.suffix, 'suffix'),
  ];
}

function newKey(used: string[], stem = 'step'): string {
  let index = used.length + 1;
  while (used.includes(`${stem}${index}`)) index += 1;
  return `${stem}${index}`;
}

function blankDevice(): RecipeStep {
  return { kind: 'device', name: '', cap: '', params: {}, dur: 5 };
}

function StepList({
  steps, earlier, lookups, readOnly, used, onChange,
}: {
  steps: FormulationFixedStep[];
  /** 排在这组前面的固定步骤：本组步骤的 after 可以引用它们 */
  earlier: FixedRef[];
  lookups: Lookups;
  readOnly: boolean;
  used: string[];
  onChange: (steps: FormulationFixedStep[]) => void;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const set = (index: number, step: FormulationFixedStep) => onChange(steps.map((row, at) => (at === index ? step : row)));
  const move = (index: number, to: number) => {
    if (to < 0 || to >= steps.length) return;
    const next = [...steps];
    const [picked] = next.splice(index, 1);
    next.splice(to, 0, picked);
    onChange(next);
  };
  return (
    <div className="stack">
      {steps.length ? null : <div className="small muted">没有固定步骤</div>}
      {steps.map((step, index) => {
        const before = [...earlier, ...steps.slice(0, index).map((row) => ({
          key: row.key, name: row.name, section: 'prefix' as const, kind: row.kind ?? 'device', cap: row.cap,
        }))];
        return (
          <div key={`${step.key}-${index}`} className="note" style={{ display: 'grid', gap: 6 }}>
            <div className="row">
              <b className="small">{index + 1}. {step.name || '（未命名）'}</b>
              <span className="tiny muted mono">{step.key}</span>
              <span className="tiny muted">
                {step.kind === 'manual' ? '人工' : step.cap || '未选能力'} ·{' '}
                {step.after ? (step.after.length ? `接在 ${step.after.join('、')} 之后` : '不接任何步骤') : '接上一步'}
              </span>
              <span style={{ flex: 1 }} />
              <button className="btn sm" onClick={() => setOpen(open === index ? null : index)}>
                {open === index ? '收起' : '展开'}
              </button>
              {readOnly ? null : (
                <>
                  <button className="btn sm" disabled={index === 0} onClick={() => move(index, index - 1)}>↑</button>
                  <button className="btn sm" disabled={index === steps.length - 1} onClick={() => move(index, index + 1)}>↓</button>
                  <button className="btn sm danger" onClick={() => onChange(steps.filter((_, at) => at !== index))}>删</button>
                </>
              )}
            </div>
            {open === index ? (
              <StepFields step={step} fixed earlier={before} lookups={lookups} readOnly={readOnly}
                onChange={(next) => set(index, next as FormulationFixedStep)} />
            ) : null}
          </div>
        );
      })}
      {readOnly ? null : (
        <div className="row">
          <button className="btn sm" onClick={() => {
            onChange([...steps, { ...blankDevice(), key: newKey(used) } as FormulationFixedStep]);
            setOpen(steps.length);
          }}>
            加设备步骤
          </button>
          <button className="btn sm" onClick={() => {
            onChange([...steps, {
              kind: 'manual', key: newKey(used), name: '', cap: '', params: {}, dur: 10,
              form: [{ key: 'ok', label: '已完成', type: 'bool' }],
            } as FormulationFixedStep]);
            setOpen(steps.length);
          }}>
            加人工步骤
          </button>
        </div>
      )}
    </div>
  );
}

/** 可选的步骤模板（全局 / 阶段 / 类别的加料后步骤）：不写就不用 */
function OptionalStep({
  step, label, lookups, readOnly, onChange, hint,
}: {
  step: RecipeStep | undefined;
  label: string;
  lookups: Lookups;
  readOnly: boolean;
  onChange: (step: RecipeStep | undefined) => void;
  hint?: string;
}) {
  if (!step) {
    return readOnly ? (
      <div className="small muted">不写（{hint ?? '用上一级的'}）</div>
    ) : (
      <button className="btn sm" onClick={() => onChange({ ...blankDevice(), name: '{material} 加料后搅拌' })}>
        写一份{label}
      </button>
    );
  }
  return (
    <div className="stack">
      <StepFields step={step} lookups={lookups} readOnly={readOnly} onChange={onChange} template />
      {readOnly ? null : (
        <div>
          <button className="btn sm danger" onClick={() => onChange(undefined)}>
            不用{label}
          </button>
        </div>
      )}
    </div>
  );
}

/* 一个步骤的字段：固定步骤（带 key、after）或步骤模板（名称里可写 {material}）。设备步骤选能力、设备方法、填参数；
   人工步骤写记录表单。参数按能力登记的类型给控件：数值、选项下拉、程序表。`dosing` 是加料步骤由方案按瓶给量的那个参数。 */
function StepFields({
  step, fixed, earlier = [], lookups, readOnly, onChange, template, dosing,
}: {
  step: RecipeStep & { key?: string; after?: string[] };
  fixed?: boolean;
  earlier?: FixedRef[];
  lookups: Lookups;
  readOnly: boolean;
  onChange: (step: RecipeStep & { key?: string; after?: string[] }) => void;
  template?: boolean;
  dosing?: string;
}) {
  const kind = step.kind ?? 'device';
  const capability = lookups.capabilities.find((row) => row.id === step.cap);
  const methods = lookups.methods.filter((row) => row.capability_id === step.cap);
  const patch = (changes: Partial<RecipeStep & { key?: string; after?: string[] }>) => onChange({ ...step, ...changes });
  const setParam = (key: string, value: number | string | ProgramRow[] | undefined) => {
    const params = { ...(step.params ?? {}) } as Record<string, number | string | ProgramRow[]>;
    if (value === undefined || value === '') delete params[key];
    else params[key] = value;
    patch({ params });
  };

  return (
    <div className="stack">
      <div className="grid cols-3">
        {fixed ? (
          <Field label="局部名 key" hint="模板内唯一；实验参数、阶段的「接在哪之后」按它引用">
            <input className="mono" value={step.key ?? ''} disabled={readOnly} onChange={(event) => patch({ key: event.target.value.trim() })} />
          </Field>
        ) : null}
        <Field label="名称" hint={template ? '可写 {material}，生成时换成物料名' : undefined}>
          <input value={step.name} disabled={readOnly} onChange={(event) => patch({ name: event.target.value })} />
        </Field>
        {fixed ? (
          <Field label="类型">
            <select
              value={kind}
              disabled={readOnly}
              onChange={(event) =>
                patch(
                  event.target.value === 'manual'
                    ? { kind: 'manual', cap: '', params: {}, method: undefined, form: step.form?.length ? step.form : [{ key: 'ok', label: '已完成', type: 'bool' }] }
                    : { kind: 'device', form: undefined },
                )
              }
            >
              <option value="device">设备</option>
              <option value="manual">人工</option>
            </select>
          </Field>
        ) : null}
        <Field label="计划时长 min">
          <NumberInput value={step.dur} disabled={readOnly} ariaLabel="计划时长" onChange={(next) => patch({ dur: next === '' ? 0 : next })} />
        </Field>
      </div>

      {lookups.sopTitles.length || step.sop_step ? (
        <Field label="对应 SOP 步骤" hint="按关联 SOP 当时生效的版本对上步骤标题">
          <select value={typeof step.sop_step === 'string' ? step.sop_step : ''} disabled={readOnly}
            onChange={(event) => patch({ sop_step: event.target.value || undefined } as Partial<RecipeStep>)}>
            <option value="">不对应</option>
            {lookups.sopTitles.map((title) => (
              <option key={title} value={title}>
                {title}
              </option>
            ))}
            {typeof step.sop_step === 'string' && step.sop_step && !lookups.sopTitles.includes(step.sop_step) ? (
              <option value={step.sop_step}>{step.sop_step}（当前 SOP 版本没有这一步）</option>
            ) : null}
          </select>
        </Field>
      ) : null}

      {fixed ? (
        <Field label="接在哪些步骤之后" hint="都不勾 = 接上一步；想让它不等任何步骤，勾「不接任何步骤」">
          <div className="dep-list">
            <label className="check">
              <input type="checkbox" disabled={readOnly} checked={Array.isArray(step.after) && !step.after.length}
                onChange={(event) => patch({ after: event.target.checked ? [] : undefined })} />
              不接任何步骤
            </label>
            {earlier.map((row) => (
              <label key={row.key} className="check">
                <input
                  type="checkbox"
                  disabled={readOnly}
                  checked={(step.after ?? []).includes(row.key)}
                  onChange={(event) => {
                    const next = new Set(step.after ?? []);
                    if (event.target.checked) next.add(row.key);
                    else next.delete(row.key);
                    patch({ after: next.size ? [...next] : undefined });
                  }}
                />
                {row.name || row.key} <span className="tiny muted mono">{row.key}</span>
              </label>
            ))}
          </div>
        </Field>
      ) : null}

      {kind === 'manual' ? (
        <ManualForm step={step} readOnly={readOnly} onChange={(form, signature) => patch({ form, requires_signature: signature || undefined })} />
      ) : (
        <>
          <div className="grid cols-2">
            <Field label="能力">
              <select value={step.cap} disabled={readOnly}
                onChange={(event) => patch({ cap: event.target.value, method: undefined, params: {} })}>
                <option value="">选择能力</option>
                {lookups.capabilities.filter((row) => !row.retired || row.id === step.cap).map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="设备方法" hint="只列这项能力已发布的方法；不引用就只按工位极限匹配">
              <select value={step.method?.id ?? ''} disabled={readOnly || !step.cap}
                onChange={(event) => patch({ method: event.target.value ? ({ id: event.target.value } as RecipeStep['method']) : undefined })}>
                <option value="">不引用</option>
                {methods.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.code} v{row.version} · {row.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          {capability
            ? Object.keys(capability.params ?? {}).map((key) => {
                const spec = paramSpec(capability, key);
                const value = (step.params ?? {})[key];
                if (key === dosing) {
                  return (
                    <div key={key} className="small muted">
                      {withUnit(spec.label, spec.unit)}：每瓶的量由方案按瓶给出（流程上写 0）
                    </div>
                  );
                }
                if (spec.type === 'program') {
                  const refs = Object.keys(capability.params ?? {})
                    .map((name) => ({ name, spec: paramSpec(capability, name) }))
                    .filter((row) => row.name !== key && (row.spec.type === 'number' || row.spec.type === 'integer'))
                    .map((row) => ({ key: row.name, label: row.spec.label, unit: row.spec.unit }));
                  return (
                    <Field key={key} label={`${spec.label}（程序表）`}>
                      <ProgramTableEditor spec={spec} value={Array.isArray(value) ? value : []} refs={refs} readOnly={readOnly}
                        onChange={(rows) => setParam(key, rows.length ? rows : undefined)} />
                    </Field>
                  );
                }
                if (spec.type === 'enum') {
                  return (
                    <Field key={key} label={spec.label}>
                      <select value={typeof value === 'string' ? value : ''} disabled={readOnly}
                        onChange={(event) => setParam(key, event.target.value || undefined)}>
                        <option value="">不填</option>
                        {spec.options.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                    </Field>
                  );
                }
                return (
                  <Field key={key} label={withUnit(spec.label, spec.unit)} hint={spec.required ? undefined : '可不填'}>
                    <NumberInput value={typeof value === 'number' ? value : ''} disabled={readOnly} ariaLabel={spec.label}
                      onChange={(next) => setParam(key, next === '' ? undefined : next)} />
                  </Field>
                );
              })
            : null}
        </>
      )}
    </div>
  );
}

function ManualForm({
  step, readOnly, onChange,
}: {
  step: RecipeStep;
  readOnly: boolean;
  onChange: (form: FormField[], signature: boolean) => void;
}) {
  const form = step.form ?? [];
  const set = (index: number, changes: Partial<FormField>) =>
    onChange(form.map((row, at) => (at === index ? { ...row, ...changes } : row)), Boolean(step.requires_signature));
  return (
    <div className="stack">
      <div className="small muted">记录表单：人工步骤完成时要填的内容</div>
      {form.map((row, index) => (
        <div key={index} className="row">
          <input className="mono" style={{ width: 110 }} value={row.key} placeholder="字段标识" disabled={readOnly}
            onChange={(event) => set(index, { key: event.target.value })} />
          <input value={row.label} placeholder="显示名称" disabled={readOnly} onChange={(event) => set(index, { label: event.target.value })} />
          <select value={row.type ?? 'text'} disabled={readOnly} onChange={(event) => set(index, { type: event.target.value as FormField['type'] })}>
            <option value="number">数值</option>
            <option value="bool">是否</option>
            <option value="text">文字</option>
          </select>
          {readOnly ? null : (
            <button className="btn sm" onClick={() => onChange(form.filter((_, at) => at !== index), Boolean(step.requires_signature))}>
              删
            </button>
          )}
        </div>
      ))}
      {readOnly ? null : (
        <div className="row">
          <button className="btn sm" onClick={() => onChange([...form, { key: `f${form.length + 1}`, label: '', type: 'text' }], Boolean(step.requires_signature))}>
            加一个字段
          </button>
          <label className="check small">
            <input type="checkbox" checked={Boolean(step.requires_signature)}
              onChange={(event) => onChange(form, event.target.checked)} />
            完成时要签名
          </label>
        </div>
      )}
    </div>
  );
}

/* ---------- 加料阶段 ---------- */

function StagesPanel({
  config, lookups, readOnly, mutate,
}: {
  config: FormulationTemplateConfig;
  lookups: Lookups;
  readOnly: boolean;
  mutate: (change: (config: FormulationTemplateConfig) => void) => void;
}) {
  const stages = config.stages ?? [];
  const all = fixedSteps(config);
  const setStage = (index: number, change: (stage: FormulationStage) => void) =>
    mutate((c) => {
      const next = c.stages ?? [];
      change(next[index]);
      c.stages = next;
    });
  const task = Boolean(config.task);
  return (
    <Panel title="加料阶段">
      <div className="small muted">
        {task
          ? '整任务方式：阶段只决定加料顺序（阶段先后、阶段内按表格列或按类别先后），上位机按这个顺序逐种加；不在这里写加料后的动作。'
          : `每个阶段按顺序加进这一阶段的各类料（物料类别在下面「加法」里指定进哪个阶段），加完再做这一阶段的固定步骤。
        阶段缺省一个接一个；不串行的阶段只接它写的「从哪几步之后开始」。`}
      </div>
      {stages.map((stage, index) => {
        // 阶段能等的：前段 + 之前各阶段的固定步骤
        const earlier = all.filter((row) => row.section === 'prefix').concat(
          stages.slice(0, index).flatMap((row) => (row.then ?? []).map((step) => ({
            key: step.key, name: step.name, section: 'stage' as const, kind: step.kind ?? 'device', cap: step.cap,
          }))),
        );
        return (
          <div key={index} className="note" style={{ display: 'grid', gap: 8 }}>
            <div className="grid cols-3">
              <Field label="阶段 key">
                <input className="mono" value={stage.key} disabled={readOnly} onChange={(event) => setStage(index, (row) => void (row.key = event.target.value.trim()))} />
              </Field>
              <Field label="名称">
                <input value={stage.label} disabled={readOnly} onChange={(event) => setStage(index, (row) => void (row.label = event.target.value))} />
              </Field>
              <Field label="加料顺序">
                <select value={stage.order ?? 'table'} disabled={readOnly}
                  onChange={(event) => setStage(index, (row) => void (row.order = event.target.value as FormulationStage['order']))}>
                  <option value="table">按表格列顺序</option>
                  <option value="routes">按下面「加法」里类别的先后</option>
                </select>
              </Field>
            </div>
            {task ? (
              readOnly ? null : (
                <div className="row">
                  <span style={{ flex: 1 }} />
                  <button className="btn sm danger" onClick={() => mutate((c) => void (c.stages = (c.stages ?? []).filter((_, at) => at !== index)))}>
                    删除阶段
                  </button>
                </div>
              )
            ) : (
            <>
            <div className="row">
              <label className="check small">
                <input type="checkbox" disabled={readOnly} checked={stage.stir_after_last !== false}
                  onChange={(event) => setStage(index, (row) => void (row.stir_after_last = event.target.checked))} />
                本阶段最后一种料加完也做加料后步骤
              </label>
              {index > 0 ? (
                <label className="check small" title="不接上一个阶段的尾巴：只接下面勾的步骤；没汇合的尾巴由后面的阶段或后段一起接上">
                  <input type="checkbox" disabled={readOnly} checked={stage.chain !== false}
                    onChange={(event) => setStage(index, (row) => {
                      if (event.target.checked) delete row.chain;
                      else row.chain = false;
                    })} />
                  接在上一个阶段之后
                </label>
              ) : null}
              <span style={{ flex: 1 }} />
              {readOnly ? null : (
                <button className="btn sm danger" onClick={() => mutate((c) => void (c.stages = (c.stages ?? []).filter((_, at) => at !== index)))}>
                  删除阶段
                </button>
              )}
            </div>
            <Field label={index === 0 || stage.chain === false ? '从哪几步之后开始' : '还要等哪几步（汇合）'}>
              <div className="dep-list">
                {earlier.map((row) => (
                  <label key={row.key} className="check">
                    <input type="checkbox" disabled={readOnly} checked={(stage.after ?? []).includes(row.key)}
                      onChange={(event) => setStage(index, (target) => {
                        const next = new Set(target.after ?? []);
                        if (event.target.checked) next.add(row.key);
                        else next.delete(row.key);
                        target.after = [...next];
                      })} />
                    {row.name || row.key}
                  </label>
                ))}
                {earlier.length ? null : <span className="tiny muted">前面还没有固定步骤</span>}
              </div>
            </Field>
            <div>
              <div className="small muted">本阶段的加料后步骤（不写就用全局的）</div>
              <OptionalStep step={stage.stir} label="本阶段的加料后步骤" lookups={lookups} readOnly={readOnly} hint="用全局的"
                onChange={(step) => setStage(index, (row) => {
                  if (step) row.stir = step;
                  else delete row.stir;
                })} />
            </div>
            <div>
              <div className="small muted">本阶段加完料之后的固定步骤</div>
              <StepList steps={stage.then ?? []} earlier={earlier} lookups={lookups} readOnly={readOnly}
                used={all.map((row) => row.key)} onChange={(steps) => setStage(index, (row) => void (row.then = steps))} />
            </div>
            </>
            )}
          </div>
        );
      })}
      {readOnly ? null : (
        <div>
          <button className="btn sm" onClick={() => mutate((c) => {
            const keys = (c.stages ?? []).map((row) => row.key);
            const added = c.task
              ? { key: newKey(keys, 'stage'), label: '新阶段' }
              : { key: newKey(keys, 'stage'), label: '新阶段', after: [], stir_after_last: true, then: [] };
            c.stages = [...(c.stages ?? []), added];
          })}>
            加一个阶段
          </button>
        </div>
      )}
    </Panel>
  );
}

/* ---------- 生成方式：逐种料生成步骤，或整任务（上位机一步投完） ---------- */

/** 切到整任务方式：去掉只有逐步编排才用得到的设置（阶段的接法与固定步骤、类别的加料步骤与搅拌、全局搅拌）；
    切回逐种料：给阶段与类别补上空的写法，由人按产线填。未保存前都可以放弃改动。 */
function setTaskMode(config: FormulationTemplateConfig, on: boolean, fixed: FixedRef[]) {
  if (on) {
    const first = fixed.find((row) => row.section !== 'stage' && row.kind === 'device');
    config.task = { step: first?.key ?? '', slots: [] };
    delete config.stir;
    config.stages = (config.stages ?? []).map((stage) => ({
      key: stage.key, label: stage.label, ...(stage.order ? { order: stage.order } : {}),
    }));
    config.routes = Object.fromEntries(Object.entries(config.routes ?? {}).map(([category, route]) => [
      category, { stage: route.stage, ...(route.not_last ? { not_last: route.not_last } : {}) },
    ]));
    return;
  }
  delete config.task;
  config.stages = (config.stages ?? []).map((stage) => ({ after: [], stir_after_last: true, then: [], ...stage }));
  config.routes = Object.fromEntries(Object.entries(config.routes ?? {}).map(([category, route]) => [
    category, { param: '', step: { ...blankDevice(), name: '{material} 加料' }, ...route },
  ]));
}

function ModePanel({
  config, fixed, lookups, readOnly, mutate,
}: {
  config: FormulationTemplateConfig;
  fixed: FixedRef[];
  lookups: Lookups;
  readOnly: boolean;
  mutate: (change: (config: FormulationTemplateConfig) => void) => void;
}) {
  const task = config.task;
  const candidates = fixed.filter((row) => row.section !== 'stage' && row.kind === 'device');
  const chosen = candidates.find((row) => row.key === task?.step);
  const capability = lookups.capabilities.find((row) => row.id === chosen?.cap);
  // 加料位：这一步能力里登记了单位的数值参数，按能力登记的顺序
  const numeric = Object.keys(capability?.params ?? {}).filter((key) => {
    const spec = paramSpec(capability, key);
    return (spec.type === 'number' || spec.type === 'integer') && Boolean(spec.unit);
  });
  return (
    <Panel title="生成方式">
      <div className="grid cols-2">
        <Field label="怎么生成流程" hint="上位机收整份实验任务、自己调度线内模组（A-Lab 一类整线）时选整任务">
          <select value={task ? 'task' : 'steps'} disabled={readOnly}
            onChange={(event) => mutate((c) => setTaskMode(c, event.target.value === 'task', fixed))}>
            <option value="steps">逐种料生成加料、搅拌步骤（中控逐步下发）</option>
            <option value="task">整任务：一个设备步骤投完一瓶的全部组分（上位机执行）</option>
          </select>
        </Field>
        {task ? (
          <Field label="整任务步骤" hint="前段或后段里的一个设备固定步骤；表格里的料都投在这一步">
            <select value={task.step} disabled={readOnly}
              onChange={(event) => mutate((c) => void (c.task = { step: event.target.value, slots: [] }))}>
              <option value="">选择步骤</option>
              {candidates.map((row) => (
                <option key={row.key} value={row.key}>
                  {row.name || row.key}
                </option>
              ))}
            </select>
          </Field>
        ) : null}
      </div>
      {task ? (
        <Field label={`加料位（已选 ${task.slots.length} 个）`}
          hint="表格里要加的料按加料顺序依次占用这些参数：第 1 种料的每瓶用量写进第 1 个加料位，依此类推">
          <div className="dep-list">
            {numeric.map((key) => (
              <label key={key} className="check">
                <input type="checkbox" disabled={readOnly} checked={task.slots.includes(key)}
                  onChange={(event) => mutate((c) => {
                    const picked = new Set(c.task?.slots ?? []);
                    if (event.target.checked) picked.add(key);
                    else picked.delete(key);
                    c.task = { step: c.task?.step ?? '', slots: numeric.filter((row) => picked.has(row)) };
                  })} />
                {withUnit(paramSpec(capability, key).label, paramSpec(capability, key).unit)}
              </label>
            ))}
            {numeric.length ? null : <span className="tiny muted">先选整任务步骤（它的能力里要有登记了单位的数值参数）</span>}
          </div>
        </Field>
      ) : null}
    </Panel>
  );
}

/* ---------- 物料类别的加法 ---------- */

function RoutesPanel({
  config, lookups, readOnly, mutate, categories,
}: {
  config: FormulationTemplateConfig;
  lookups: Lookups;
  readOnly: boolean;
  mutate: (change: (config: FormulationTemplateConfig) => void) => void;
  categories: string[];
}) {
  const routes = Object.entries(config.routes ?? {});
  const [adding, setAdding] = useState('');
  const setRoute = (category: string, change: (route: FormulationRoute) => void) =>
    mutate((c) => {
      const next = { ...(c.routes ?? {}) };
      change(next[category]);
      c.routes = next;
    });
  const rename = (from: string, to: string) =>
    mutate((c) => {
      // 保持 routes 里的先后（「按类别先后」加料按它排）
      c.routes = Object.fromEntries(Object.entries(c.routes ?? {}).map(([key, value]) => [key === from ? to : key, value]));
    });
  const task = Boolean(config.task);
  return (
    <Panel title="物料类别的加法">
      <div className="small muted">
        {task
          ? '整任务方式：类别只决定进哪个阶段（加料顺序）与「不能是最后一种」的检查；这类料怎么加由上位机定。'
          : '配方表每一列是一种试剂，按它在物料主数据里的类别决定进哪个阶段、用哪台设备怎么加。'}
        类别在「试剂耗材 → 物料主数据」里维护。
      </div>
      {routes.map(([category, route], index) => {
        const capability = lookups.capabilities.find((row) => row.id === route.step?.cap);
        const numeric = Object.keys(capability?.params ?? {}).filter((key) => {
          const type = paramSpec(capability, key).type;
          return type === 'number' || type === 'integer';
        });
        return (
          <div key={index} className="note" style={{ display: 'grid', gap: 8 }}>
            <div className="grid cols-3">
              <Field label="物料类别">
                <input value={category} disabled={readOnly} list="route-categories" onChange={(event) => rename(category, event.target.value)} />
              </Field>
              <Field label="进哪个阶段">
                <select value={route.stage} disabled={readOnly} onChange={(event) => setRoute(category, (row) => void (row.stage = event.target.value))}>
                  <option value="">选择阶段</option>
                  {(config.stages ?? []).map((stage) => (
                    <option key={stage.key} value={stage.key}>
                      {stage.label}
                    </option>
                  ))}
                </select>
              </Field>
              {task ? null : (
              <Field label="用量写到哪个参数" hint="每瓶的量由方案按瓶给出">
                <select value={route.param ?? ''} disabled={readOnly || !route.step?.cap} onChange={(event) => setRoute(category, (row) => void (row.param = event.target.value))}>
                  <option value="">选择参数</option>
                  {numeric.map((key) => (
                    <option key={key} value={key}>
                      {withUnit(paramSpec(capability, key).label, paramSpec(capability, key).unit)}
                    </option>
                  ))}
                </select>
              </Field>
              )}
            </div>
            <div className="row">
              {task ? null : (
              <label className="check small">
                <input type="checkbox" disabled={readOnly} checked={route.stir_after !== false}
                  onChange={(event) => setRoute(category, (row) => void (row.stir_after = event.target.checked))} />
                加完做加料后步骤（不是阶段最后一种时）
              </label>
              )}
              <label className="check small">
                <input type="checkbox" disabled={readOnly} checked={Boolean(route.not_last)}
                  onChange={(event) => setRoute(category, (row) => {
                    if (event.target.checked) row.not_last = '这类料加完要紧接着加下一种';
                    else delete row.not_last;
                  })} />
                不能是一瓶在本阶段加的最后一种
              </label>
              {route.not_last ? (
                <input style={{ minWidth: 240 }} value={typeof route.not_last === 'string' ? route.not_last : ''} disabled={readOnly}
                  placeholder="原因（会写进导入问题里）" onChange={(event) => setRoute(category, (row) => void (row.not_last = event.target.value || true))} />
              ) : null}
              <span style={{ flex: 1 }} />
              {readOnly ? null : (
                <button className="btn sm danger" onClick={() => mutate((c) => {
                  const next = { ...(c.routes ?? {}) };
                  delete next[category];
                  c.routes = next;
                })}>
                  删除这类
                </button>
              )}
            </div>
            {task ? null : (
            <>
            <div>
              <div className="small muted">加料步骤（名称里的 {'{material}'} 换成物料名）</div>
              <StepFields step={route.step ?? blankDevice()} lookups={lookups} readOnly={readOnly} template dosing={route.param}
                onChange={(step) => setRoute(category, (row) => void (row.step = step))} />
            </div>
            <div>
              <div className="small muted">这类料的加料后步骤（不写就用阶段的或全局的）</div>
              <OptionalStep step={route.stir} label="这类料的加料后步骤" lookups={lookups} readOnly={readOnly} hint="用阶段的或全局的"
                onChange={(step) => setRoute(category, (row) => {
                  if (step) row.stir = step;
                  else delete row.stir;
                })} />
            </div>
            </>
            )}
          </div>
        );
      })}
      <datalist id="route-categories">
        {categories.map((category) => (
          <option key={category} value={category} />
        ))}
      </datalist>
      {readOnly ? null : (
        <div className="row">
          <input value={adding} list="route-categories" placeholder="物料类别，如 溶剂" onChange={(event) => setAdding(event.target.value)} />
          <button
            className="btn sm"
            disabled={!adding.trim() || adding.trim() in (config.routes ?? {})}
            onClick={() => {
              const category = adding.trim();
              mutate((c) => {
                c.routes = {
                  ...(c.routes ?? {}),
                  [category]: c.task
                    ? { stage: (c.stages ?? [])[0]?.key ?? '' }
                    : { stage: (c.stages ?? [])[0]?.key ?? '', param: '', step: { ...blankDevice(), name: '{material} 加料' } },
                };
              });
              setAdding('');
            }}
          >
            加一类物料
          </button>
        </div>
      )}
    </Panel>
  );
}

/* ---------- 实验参数与逐瓶参数 ---------- */

function ParamsPanel({
  config, fixed, lookups, readOnly, mutate, specs,
}: {
  config: FormulationTemplateConfig;
  fixed: FixedRef[];
  lookups: Lookups;
  readOnly: boolean;
  mutate: (change: (config: FormulationTemplateConfig) => void) => void;
  specs: Record<string, { type: string; options: string[]; unit: string }>;
}) {
  const devices = fixed.filter((row) => row.kind === 'device' && row.cap);
  const paramsOf = (stepKey: string) => {
    const capability = lookups.capabilities.find((row) => row.id === devices.find((item) => item.key === stepKey)?.cap);
    return Object.keys(capability?.params ?? {})
      .map((key) => ({ key, spec: paramSpec(capability, key) }))
      .filter((row) => row.spec.type !== 'program');
  };
  const experiment = config.experiment_params ?? [];
  const rows = config.row_params ?? [];
  const setExperiment = (index: number, changes: Partial<FormulationExperimentParam>) =>
    mutate((c) => void (c.experiment_params = (c.experiment_params ?? []).map((row, at) => (at === index ? { ...row, ...changes } : row))));
  const setRow = (index: number, changes: Partial<FormulationRowParam>) =>
    mutate((c) => void (c.row_params = (c.row_params ?? []).map((row, at) => (at === index ? { ...row, ...changes } : row))));
  const targetFields = (
    value: { step: string; param: string },
    onChange: (changes: { step?: string; param?: string; unit?: string }) => void,
  ) => (
    <>
      <select value={value.step} disabled={readOnly} aria-label="作用于哪一步"
        onChange={(event) => onChange({ step: event.target.value, param: '' })}>
        <option value="">作用于哪一步</option>
        {devices.map((row) => (
          <option key={row.key} value={row.key}>
            {row.name || row.key}
          </option>
        ))}
      </select>
      <select value={value.param} disabled={readOnly || !value.step} aria-label="哪个参数"
        onChange={(event) => {
          const picked = paramsOf(value.step).find((row) => row.key === event.target.value);
          onChange({ param: event.target.value, unit: picked?.spec.unit ?? '' });
        }}>
        <option value="">哪个参数</option>
        {paramsOf(value.step).map((row) => (
          <option key={row.key} value={row.key}>
            {withUnit(row.spec.label, row.spec.unit)}
          </option>
        ))}
      </select>
    </>
  );
  const defaultField = (key: string, value: number | string | null | undefined, onChange: (next: number | string | undefined) => void) =>
    specs[key]?.type === 'enum' ? (
      <select value={typeof value === 'string' ? value : ''} disabled={readOnly} aria-label="缺省值"
        onChange={(event) => onChange(event.target.value || undefined)}>
        <option value="">缺省</option>
        {specs[key].options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    ) : (
      <NumberInput value={typeof value === 'number' ? value : ''} disabled={readOnly} ariaLabel="缺省值"
        onChange={(next) => onChange(next === '' ? undefined : next)} />
    );

  return (
    <Panel title="每次实验可改的参数">
      <div className="small muted">实验参数：整批一个值，导入时可以改（如分装瓶数、每瓶分装量）。作用于某个固定设备步骤的参数。</div>
      {experiment.map((row, index) => (
        <div key={index} className="row">
          <input className="mono" style={{ width: 100 }} value={row.key} placeholder="key" disabled={readOnly}
            onChange={(event) => setExperiment(index, { key: event.target.value.trim() })} />
          <input value={row.label} placeholder="显示名称" disabled={readOnly} onChange={(event) => setExperiment(index, { label: event.target.value })} />
          {targetFields(row, (changes) => setExperiment(index, changes))}
          <span className="small mono">{row.unit}</span>
          {defaultField(row.key, row.default, (next) => setExperiment(index, { default: next ?? 0 }))}
          {readOnly ? null : (
            <button className="btn sm" onClick={() => mutate((c) => void (c.experiment_params = (c.experiment_params ?? []).filter((_, at) => at !== index)))}>
              删
            </button>
          )}
        </div>
      ))}
      {readOnly ? null : (
        <div>
          <button className="btn sm" onClick={() => mutate((c) => void (c.experiment_params = [
            ...(c.experiment_params ?? []), { key: newKey((c.experiment_params ?? []).map((row) => row.key), 'param'), label: '', step: '', param: '', unit: '', default: 0 },
          ]))}>
            加一个实验参数
          </button>
        </div>
      )}

      <div className="small muted" style={{ marginTop: 12 }}>
        逐瓶参数列：表格里用量以外的列（如终混温度），每瓶一个值，按孔位下发给它指向的步骤；同配方不同参数是不同条件。
        空白格用缺省值，不给缺省值就每瓶都要写。
      </div>
      {rows.map((row, index) => (
        <div key={index} className="row">
          <input className="mono" style={{ width: 100 }} value={row.key} placeholder="key" disabled={readOnly}
            onChange={(event) => setRow(index, { key: event.target.value.trim() })} />
          <input value={row.header} placeholder="表格里的列名" disabled={readOnly} onChange={(event) => setRow(index, { header: event.target.value })} />
          {targetFields(row, (changes) => setRow(index, changes))}
          <span className="small mono">{row.unit}</span>
          {defaultField(row.key, row.default, (next) => setRow(index, { default: next ?? null }))}
          {readOnly ? null : (
            <button className="btn sm" onClick={() => mutate((c) => void (c.row_params = (c.row_params ?? []).filter((_, at) => at !== index)))}>
              删
            </button>
          )}
        </div>
      ))}
      {readOnly ? null : (
        <div>
          <button className="btn sm" onClick={() => mutate((c) => void (c.row_params = [
            ...(c.row_params ?? []), { key: newKey([...(c.row_params ?? []), ...(c.experiment_params ?? [])].map((row) => row.key), 'col'), header: '', step: '', param: '', unit: '' },
          ]))}>
            加一个逐瓶参数列
          </button>
        </div>
      )}
    </Panel>
  );
}

function VolumeCheckPanel({
  config, readOnly, mutate,
}: {
  config: FormulationTemplateConfig;
  readOnly: boolean;
  mutate: (change: (config: FormulationTemplateConfig) => void) => void;
}) {
  const check = config.volume_check;
  const keys = [...(config.experiment_params ?? []), ...(config.row_params ?? [])].map((row) => ({
    key: row.key, label: ('label' in row && row.label) || ('header' in row ? row.header : '') || row.key,
  }));
  return (
    <Panel title="分装量核对">
      <label className="check small">
        <input type="checkbox" disabled={readOnly} checked={Boolean(check)}
          onChange={(event) => mutate((c) => {
            if (event.target.checked) c.volume_check = { bottles: keys[0]?.key ?? '', volume: keys[1]?.key ?? '', reserve: 0 };
            else delete c.volume_check;
          })} />
        导入时核对每瓶母液够不够分装：分装瓶数 × 每瓶分装量 + 母瓶留样放不下就不能导入
      </label>
      {check ? (
        <div className="grid cols-2">
          <Field label="分装瓶数取自">
            <select value={check.bottles} disabled={readOnly} onChange={(event) => mutate((c) => void (c.volume_check!.bottles = event.target.value))}>
              {keys.map((row) => (
                <option key={row.key} value={row.key}>
                  {row.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="每瓶分装量取自">
            <select value={check.volume} disabled={readOnly} onChange={(event) => mutate((c) => void (c.volume_check!.volume = event.target.value))}>
              {keys.map((row) => (
                <option key={row.key} value={row.key}>
                  {row.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="缺省密度 g/mL" hint="物料主数据没登记密度（1 mL = x g）的按它；取偏大的值，核对偏保守；可不填">
            <NumberInput value={check.density ?? ''} disabled={readOnly} ariaLabel="缺省密度"
              onChange={(next) => mutate((c) => {
                if (next === '') delete c.volume_check!.density;
                else c.volume_check!.density = next;
              })} />
          </Field>
          <Field label="母瓶至少留 mL">
            <NumberInput value={check.reserve ?? 0} disabled={readOnly} ariaLabel="母瓶留样"
              onChange={(next) => mutate((c) => void (c.volume_check!.reserve = next === '' ? 0 : next))} />
          </Field>
        </div>
      ) : null}
    </Panel>
  );
}

/* ---------- 试算 ---------- */

function TrialPanel({ draft }: { draft: Draft }) {
  const [table, setTable] = useState<{ filename: string; table: (string | number | null)[][]; warnings: string[] } | null>(null);
  const [result, setResult] = useState<FormulationCheck | null>(null);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const run = async (sheet = table) => {
    if (!sheet) return;
    setPending(true);
    setError('');
    try {
      setResult(await api.post<FormulationCheck>(`${BASE}/check`, {
        config: draft.config, name: draft.name || '模板试算', description: draft.description, filename: sheet.filename, table: sheet.table,
      }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setPending(false);
    }
  };
  const preview = result?.preview;
  return (
    <Panel title="用一张配方表试算">
      <div className="small muted">按还没保存的配置生成一次流程与方案，看看步骤对不对；不写库、不登记瓶子。</div>
      <input
        type="file"
        accept=".xlsx,.csv"
        onChange={async (event) => {
          const file = event.target.files?.[0];
          if (!file) return;
          setError('');
          try {
            const sheet = await api.upload<{ filename: string; table: (string | number | null)[][]; warnings: string[] }>(`${BASE}/table`, file);
            setTable(sheet);
            await run(sheet);
          } catch (caught) {
            setError(caught instanceof Error ? caught.message : String(caught));
          }
        }}
      />
      {table ? (
        <button className="btn sm" disabled={pending} onClick={() => run()}>
          按当前配置重新试算
        </button>
      ) : null}
      {error ? <div className="note bad">{error}</div> : null}
      {preview ? (
        <div className="stack">
          {preview.issues.length ? <Blocked reasons={preview.issues} /> : <div className="small">这张表能导入。</div>}
          {preview.warnings.length ? (
            <ul className="issue-list small warn-text">
              {preview.warnings.slice(0, 8).map((text) => (
                <li key={text}>{text}</li>
              ))}
            </ul>
          ) : null}
          <div className="small muted">
            {preview.rows.length} 瓶 · {preview.plan.design_points.length} 个条件 × {preview.plan.repeats} 次重复 · {preview.steps.length} 步
          </div>
          <ol className="small" style={{ maxHeight: 360, overflowY: 'auto' }}>
            {preview.steps.map((step) => (
              <li key={step.step_id}>
                {step.name}
                {step.applies_to ? <span className="tiny muted">（按瓶执行）</span> : null}
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </Panel>
  );
}
