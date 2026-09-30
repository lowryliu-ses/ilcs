/* 配方导入：选配液模板 → 上传实验表格（.xlsx / .csv）→ 预览生成的流程与方案 → 生成草稿。

   表格一行一瓶：一列瓶身序列号，其余列是各试剂每瓶的用量。模板决定怎么把它翻译成流程：
   物料类别 → 进哪个阶段、用哪台设备怎么加、加完是否紧跟搅拌；前后的固定步骤（物料准备段、测试段）照抄。
   每瓶的量不写进流程，而是成为方案的因子（按孔位下发），所以量变了流程可以沿用，只新建方案。

   表格解析与生成都在服务端做，这里只渲染服务端给的预览；导入时服务端按同一份表格重新生成一遍再写库，
   不信任这里看到的预览。导入只建草稿（流程草稿或沿用已有流程、方案草稿、登记瓶子），
   评审、批准、发布、锁定方案仍按原来的受控流程由人走。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, api } from '../../shared/api';
import { useMutation, useQuery } from '../../shared/query';
import { useSession } from '../../shared/session';
import { PLAN_STATE_LABEL, RECIPE_STATE_LABEL } from '../../shared/types';
import type {
  CapabilityRow, FormulationImportResult, FormulationPreview, FormulationTemplate, FormulationTemplateConfig,
  FormulationVolumeCheck, RecipeStep,
} from '../../shared/types';
import { Blocked, Empty, Field, ListState, Metric, NumberInput, Panel, useToast } from '../../shared/ui';
import { STEP_KINDS, kindOf } from '../recipes/rules';

type Cell = string | number | null;

/** 一次上传：原始表格留在页面里，改实验参数时连同它一起发给 /preview 与 /import */
type Sheet = {
  /** 每次上传一个新值，区分「同一份文件重新上传」与「参数变了」 */
  id: number;
  templateId: string;
  filename: string;
  table: Cell[][];
  /** 上传时服务端按实验参数缺省值生成的预览 */
  base: FormulationPreview;
  /** base 对应的实验参数（序列化后） */
  paramsKey: string;
};

/** 当前参数下的预览：上传时那份、刷新回来的那份，或刷新失败的原因 */
type Current = { preview?: FormulationPreview; error?: ApiError };

const COLUMN_KIND: Record<string, string> = { serial: '序列号', reagent: '试剂', ignored: '忽略' };
const kindLabel = (step: RecipeStep) => STEP_KINDS.find(([value]) => value === kindOf(step))?.[1] ?? String(step.kind);

function defaultsOf(config: FormulationTemplateConfig | undefined): Record<string, number> {
  return Object.fromEntries((config?.experiment_params ?? []).map((row) => [row.key, row.default]));
}

/** 服务端 422 带回的问题清单（模板配置、表格或生成结果的全部问题） */
function problemsOf(error: ApiError | undefined): string[] {
  const detail = (error?.payload as { detail?: { problems?: unknown; issues?: unknown } } | undefined)?.detail;
  const list = detail?.problems ?? detail?.issues;
  return Array.isArray(list) ? list.map((item) => (typeof item === 'string' ? item : JSON.stringify(item))) : [];
}

/* 解析结果里没带原始表格时，从列识别与瓶子表拼回一份：被忽略的列内容丢了，
   但它们本来就不参与生成，服务端重新生成的结果一致。 */
function tableOf(preview: FormulationPreview): Cell[][] {
  const header: Cell[] = preview.columns.map((column) => column.header);
  const body = preview.rows.map((row) =>
    preview.columns.map((column): Cell =>
      column.kind === 'serial' ? row.serial : column.kind === 'reagent' ? row.amounts[column.name] ?? null : null,
    ),
  );
  return [header, ...body];
}

const num = (value: number | undefined) => (value === undefined || value === null ? '—' : String(Number(value.toFixed(6))));

export function FormulationImportPage() {
  const { can } = useSession();
  const toast = useToast();
  const allowed = can('recipe.edit');
  const templates = useQuery<FormulationTemplate[]>(allowed ? 'formulation-templates' : null, () =>
    api.get<FormulationTemplate[]>('/formulation-templates'),
  );
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  const active = (templates.data ?? []).filter((row) => row.state === 'active');

  const [picked, setPicked] = useState('');
  const templateId = picked || active[0]?.id || '';
  const detail = useQuery<FormulationTemplate>(templateId ? `formulation-templates:${templateId}` : null, () =>
    api.get<FormulationTemplate>(`/formulation-templates/${templateId}`),
  );
  const template = detail.data?.id === templateId ? detail.data : undefined;
  const config = template?.config;
  /* 解析请求在途时换了模板，回来的结果要认得出是旧模板的：按渲染时最新的模板比对 */
  const currentTemplate = useRef(templateId);
  currentTemplate.current = templateId;
  /* 没手选时模板取列表第一个；列表一刷新（切回标签页会全部重取）第一个可能变。
     列表头一次到手就把它记成选中的，已经解析好的表格不会因为刷新换了模板。 */
  const firstActive = active[0]?.id ?? '';
  const pickedActive = !picked || active.some((row) => row.id === picked);
  useEffect(() => {
    if (!picked && firstActive) setPicked(firstActive);
    // 选中的模板退役了（或刷新后不在可用列表里）：回到第一个可用模板；已解析的表格随模板变化作废
    else if (templates.data && !pickedActive) setPicked(firstActive);
  }, [picked, firstActive, templates.data, pickedActive]);

  const [sheet, setSheet] = useState<Sheet | null>(null);
  const [params, setParams] = useState<Record<string, number | ''>>({});
  const [planName, setPlanName] = useState('');
  const [result, setResult] = useState<FormulationImportResult | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const uploads = useRef(0);

  /* 换模板或模板的实验参数变了：参数回到缺省值 */
  const defaults = useMemo(() => defaultsOf(config), [config]);
  const defaultsKey = JSON.stringify(defaults);
  useEffect(() => {
    setParams(JSON.parse(defaultsKey) as Record<string, number>);
  }, [templateId, defaultsKey]);

  /* 留空的参数按缺省值：发给服务端的永远是完整的一组数，预览与导入用的是同一组 */
  const effective = useMemo(
    () => Object.fromEntries(Object.entries({ ...defaults, ...params }).map(([key, value]) => [key, value === '' ? defaults[key] : value])),
    [defaults, params],
  ) as Record<string, number>;
  const paramsKey = JSON.stringify(effective);

  /* 参数输入停下 400 ms 再刷新预览，免得每敲一个字就生成一遍 */
  const [settled, setSettled] = useState(paramsKey);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(paramsKey), 400);
    return () => window.clearTimeout(timer);
  }, [paramsKey]);

  /* 参数不是上传时的那一组就调 /preview。自己管请求而不用 useQuery：
     连续改参数时先发的请求可能后回来，按「这份结果对应哪一次上传、哪组参数」认领，旧的直接丢掉。 */
  const [fresh, setFresh] = useState<{ key: string; preview?: FormulationPreview; error?: ApiError } | null>(null);
  const freshKey = sheet ? `${sheet.id}|${settled}` : '';
  const needsRefresh = !!sheet && settled !== sheet.paramsKey;
  useEffect(() => {
    if (!sheet || !needsRefresh) return;
    let cancelled = false;
    const key = freshKey;
    api
      .post<FormulationPreview>(`/formulation-templates/${sheet.templateId}/preview`, {
        filename: sheet.filename, table: sheet.table, params: JSON.parse(settled),
      })
      .then((preview) => !cancelled && setFresh({ key, preview }))
      .catch((error: unknown) => {
        if (!cancelled) setFresh({ key, error: error instanceof ApiError ? error : new ApiError(0, { detail: String(error) }) });
      });
    return () => {
      cancelled = true;
    };
  }, [sheet, needsRefresh, freshKey, settled]);

  const current: Current | null = !sheet ? null : !needsRefresh ? { preview: sheet.base } : fresh?.key === freshKey ? fresh : null;
  const preview = current?.preview;
  const refreshing = !!sheet && (paramsKey !== settled || !current);

  const parse = useMutation(
    async (file: File) => {
      // 解析用的模板与它的缺省参数随结果一起带回：表格只对解析它的那个模板有效
      const parsedWith = templateId;
      const parsedDefaults = defaults;
      return {
        file,
        parsedWith,
        parsedDefaults,
        preview: await api.upload<FormulationPreview>(`/formulation-templates/${parsedWith}/parse`, file),
      };
    },
    {
      invalidates: [],
      onSuccess: ({ file, parsedWith, parsedDefaults: defaults, preview: parsed }) => {
        // 解析期间换了模板：这份结果是按旧模板解析的，丢掉，不让它挂到新模板名下
        if (parsedWith !== currentTemplate.current) return;
        uploads.current += 1;
        setSheet({
          id: uploads.current,
          templateId: parsedWith,
          // 预览与导入的请求体限 200 字符；文件名只用于命名与审计，截断无妨
          filename: (parsed.filename || file.name).slice(0, 200),
          table: parsed.table ?? tableOf(parsed),
          base: parsed,
          // 服务端解析时用的是模板缺省值
          paramsKey: JSON.stringify(defaults),
        });
        setParams(defaults);
        setSettled(JSON.stringify(defaults));
        setFresh(null);
        setResult(null);
      },
    },
  );

  const commit = useMutation(
    () =>
      api.post<FormulationImportResult>(
        `/formulation-templates/${sheet?.templateId}/import`,
        { filename: sheet?.filename, table: sheet?.table, params: effective, plan_name: planName.trim() },
        true,
      ),
    {
      invalidates: ['recipes', 'plans', 'samples', 'dashboard', 'audit'],
      onSuccess: (done) => {
        setResult(done);
        toast.push(done.recipe.reused ? '已生成方案草稿，流程沿用已有版本' : '已生成流程草稿与方案草稿');
      },
    },
  );

  /* 表格只对解析它的模板有效：模板一变（手选或列表变化）就作废，要按新模板重新上传 */
  useEffect(() => {
    if (sheet && sheet.templateId !== templateId) {
      setSheet(null);
      setResult(null);
    }
  }, [sheet, templateId]);

  /* 导入成功后参数又改了，是另一次导入（另建一份方案），结果卡片收起。
     导入请求在途时输入是锁住的，所以回来时的参数一定就是发出去的那组，结果不会刚到就被收起。 */
  const importedKey = useRef('');
  useEffect(() => {
    if (result && importedKey.current && importedKey.current !== `${sheet?.id}|${paramsKey}|${planName.trim()}`) setResult(null);
  }, [result, sheet?.id, paramsKey, planName]);

  if (!allowed) {
    return (
      <div className="page">
        <div className="page-head">
          <h1>配方导入</h1>
        </div>
        <Empty>当前角色不能导入配方表（需要实验流程编辑权限）</Empty>
      </div>
    );
  }

  const missing = [
    !can('plan.edit') ? '当前角色不能建实验方案（需要方案编辑权限）' : '',
    !can('sample.register') ? '当前角色不能登记样本（导入要按序列号登记瓶子）' : '',
  ].filter(Boolean);
  const issues = preview?.issues ?? [];
  const blockers = [
    ...missing,
    ...(issues.length ? [`预览有 ${issues.length} 个问题，改好表格后重新上传`] : []),
    ...(refreshing ? ['预览正在按新的实验参数刷新'] : []),
    ...(current?.error ? ['按当前实验参数生成预览失败'] : []),
  ];
  // 重新上传的表格还在解析：这时导入的是旧表，回来的解析结果又会把结果卡片收起，所以一并挡住
  const canImport = !!preview && !blockers.length && !commit.pending && !parse.pending && !result;

  const pickFile = (file: File) => {
    commit.clearError();
    // 失败原因（含逐条问题）显示在页面上，不另弹提示
    parse.run(file).catch(() => undefined);
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>配方导入</h1>
          <span className="small muted">
            上传配方表（一行一瓶：瓶身序列号 + 各试剂每瓶用量），按配液模板生成流程草稿与方案草稿；量变了流程可以沿用，只新建方案。
          </span>
        </div>
        <div className="filters">
          <select
            value={templateId}
            aria-label="配液模板"
            disabled={parse.pending || commit.pending}
            onChange={(event) => {
              setPicked(event.target.value);
              setSheet(null);
              setResult(null);
            }}
          >
            {active.length ? null : <option value="">没有可用的配液模板</option>}
            {active.map((row) => (
              <option key={row.id} value={row.id}>
                {row.code} {row.name}
              </option>
            ))}
          </select>
          <input
            ref={fileInput}
            type="file"
            accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            style={{ display: 'none' }}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              if (file) pickFile(file);
            }}
          />
          <button
            className="btn primary"
            disabled={!template || parse.pending || commit.pending || !!template.check?.problems.length}
            title={template?.check?.problems.length ? '模板配置有问题，不能用它导入' : undefined}
            onClick={() => fileInput.current?.click()}
          >
            {parse.pending ? '解析中…' : sheet ? '重新上传配方表' : '上传配方表'}
          </button>
        </div>
      </div>

      <ListState
        loading={templates.loading && !templates.data}
        error={templates.error}
        empty={!!templates.data && !active.length}
        emptyText="还没有可用的配液模板：模板决定各类试剂怎么加、前后有哪些固定步骤，由流程负责人登记"
      />
      {template?.check?.problems.length ? (
        <div className="note bad">
          模板 {template.code} 的配置有问题，不能用它导入：
          <Blocked reasons={template.check.problems} />
        </div>
      ) : null}
      {parse.error ? (
        <div className="note bad">
          配方表解析失败：{parse.error.message}
          <Blocked reasons={problemsOf(parse.error)} />
        </div>
      ) : null}

      <div className="grid cols-2-1">
        <div className="grid">
          {!sheet ? (
            <Panel title="配方表">
              <Empty>
                {templateId
                  ? '上传 .xlsx 或 .csv：第一行表头，一列是瓶身序列号，其余列表头写试剂名与单位，如 EC (g)；这一瓶不加的料填 0——同一列有的填了、有的空着不能导入，整列空白表示这次不用这种料'
                  : '先选配液模板'}
              </Empty>
            </Panel>
          ) : (
            <PreviewPanels
              sheet={sheet}
              preview={preview}
              config={config}
              capabilities={capabilities.data ?? []}
              error={current?.error}
              refreshing={refreshing}
            />
          )}

          {sheet ? (
            <Panel title="生成流程与方案">
              {(config?.experiment_params ?? []).length ? (
                <div className="grid cols-3">
                  {(config?.experiment_params ?? []).map((row) => (
                    <Field key={row.key} label={`${row.label} ${row.unit}`} hint={`缺省 ${row.default}；留空按缺省值`}>
                      <NumberInput
                        value={params[row.key] ?? ''}
                        ariaLabel={row.label}
                        disabled={commit.pending}
                        onChange={(next) => setParams((values) => ({ ...values, [row.key]: next }))}
                      />
                    </Field>
                  ))}
                </div>
              ) : null}
              <Field label="方案名称" hint={`留空为「${template?.name ?? '模板名'} · ${sheet.filename}」`}>
                <input
                  value={planName}
                  maxLength={200}
                  disabled={commit.pending}
                  onChange={(event) => setPlanName(event.target.value)}
                />
              </Field>
              {!result ? <Blocked reasons={blockers} /> : null}
              {commit.error ? (
                <div className="note bad">
                  导入失败：{commit.error.message}
                  <Blocked reasons={problemsOf(commit.error)} />
                </div>
              ) : null}
              <div className="row-end">
                <button
                  className="btn primary"
                  disabled={!canImport}
                  title={blockers.join('；') || undefined}
                  onClick={() => {
                    importedKey.current = `${sheet.id}|${paramsKey}|${planName.trim()}`;
                    commit.run().catch(() => undefined);
                  }}
                >
                  {commit.pending ? '生成中…' : result ? '已生成' : '生成流程与方案'}
                </button>
              </div>
              <div className="small muted">
                只建草稿：流程结构与已有流程完全一致时沿用那一版，否则新建流程草稿；方案总是新建草稿；表格里的序列号登记为样本（已登记的沿用）。
              </div>
            </Panel>
          ) : null}

          {result ? <ResultCard result={result} /> : null}
        </div>

        <TemplateRules template={template} loading={!!templateId && detail.loading && !template} />
      </div>
    </div>
  );
}

/* ---------- 预览 ---------- */

function PreviewPanels({
  sheet,
  preview,
  config,
  capabilities,
  error,
  refreshing,
}: {
  sheet: Sheet;
  /** 当前参数下的预览；参数刚改、新预览还没回来时是 undefined */
  preview: FormulationPreview | undefined;
  config: FormulationTemplateConfig | undefined;
  capabilities: CapabilityRow[];
  error?: ApiError;
  refreshing: boolean;
}) {
  // 参数刷新期间先显示上传时的那份，免得整页闪空；方案摘要与导入按钮以刷新后的为准
  const shown = preview ?? sheet.base;
  // 读文件时的提醒（隐藏行、隐藏工作表）只有上传解析时才有：改参数后的预览只提交表格，照样留着它们
  const fileWarnings = (sheet.base.sheet_warnings ?? []).filter((text) => !shown.warnings.includes(text));
  const warnings = [...fileWarnings, ...shown.warnings];
  const stageLabel = Object.fromEntries((config?.stages ?? []).map((row) => [row.key, row.label]));
  const capName = Object.fromEntries(capabilities.map((row) => [row.id, row.name]));
  const reagentColumns = shown.columns.filter((column) => column.kind === 'reagent');
  const position = Object.fromEntries(shown.steps.map((step, at) => [step.step_id ?? '', at + 1]));
  const plan = shown.plan;

  return (
    <>
      {error ? (
        <div className="note bad">
          按当前实验参数生成预览失败：{error.message}
          <Blocked reasons={problemsOf(error)} />
        </div>
      ) : null}
      {shown.issues.length ? (
        <div className="note bad">
          <b>问题（{shown.issues.length}）：改好表格后重新上传，有问题不能导入</b>
          <Blocked reasons={shown.issues} />
        </div>
      ) : null}
      {warnings.length ? (
        <div className="note warn">
          <b>提醒（{warnings.length}）</b>
          <ul className="small">
            {warnings.map((text, at) => (
              <li key={at}>{text}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <Panel
        title="方案摘要"
        aside={refreshing ? <span className="warn-text">按新参数刷新中…</span> : <span className="mono">{sheet.filename}</span>}
      >
        <div className="metrics">
          <Metric label="配方数" value={plan?.design_points?.length ?? 0} hint="完全相同的行算同一配方" />
          <Metric label="重复数" value={plan?.repeats ?? 0} hint="每个配方的瓶数" />
          <Metric label="瓶数" value={plan?.sample_ids?.length ?? shown.rows.length} />
          <Metric label="因子数" value={plan?.factors?.length ?? 0} hint="每种试剂一个 + 实验参数" />
          <Metric label="流程步骤" value={shown.steps.length} />
        </div>
        {shown.recipe?.name ? (
          <div className="small">
            流程 <b>{shown.recipe.name}</b>
            <span className="muted">（结构与已有流程完全一致时沿用那一版，导入时判定）</span>
          </div>
        ) : null}
        {plan?.goal ? <div className="small muted">{plan.goal}</div> : null}
        {plan?.factors?.length ? (
          <table>
            <thead>
              <tr>
                <th>因子</th>
                <th>单位</th>
                <th>水平</th>
                <th>作用于</th>
              </tr>
            </thead>
            <tbody>
              {plan.factors.map((factor, at) => (
                <tr key={at}>
                  <td>
                    {factor.name}
                    {factor.material ? <span className="tag" style={{ marginLeft: 6 }}>投料</span> : null}
                  </td>
                  <td className="mono small">{factor.unit}</td>
                  <td className="mono small">{factor.levels.join('、')}</td>
                  <td className="small">
                    {factor.target
                      ? `第 ${position[factor.target.step_id] ?? '?'} 步 · ${factor.target.param}`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      <Panel title={`列识别（${shown.columns.length}）`} flush>
        <table>
          <thead>
            <tr>
              <th>表头</th>
              <th>识别为</th>
              <th>试剂 / 单位</th>
              <th>类别</th>
              <th>阶段</th>
            </tr>
          </thead>
          <tbody>
            {shown.columns.map((column, at) => (
              <tr key={at} className={column.kind === 'ignored' ? 'muted' : undefined}>
                <td className="mono small">{column.header}</td>
                <td>
                  <span className={column.kind === 'ignored' ? 'tag warn' : 'tag'}>{COLUMN_KIND[column.kind] ?? column.kind}</span>
                </td>
                <td className="small">
                  {column.kind === 'reagent' ? `${column.name} · ${column.unit}` : column.kind === 'serial' ? '瓶身序列号' : '—'}
                </td>
                <td className="small">{column.category || '—'}</td>
                <td className="small">{column.stage ? stageLabel[column.stage] ?? column.stage : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title={`瓶子（${shown.rows.length}）`} flush>
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th className="num">行</th>
                <th>序列号</th>
                {reagentColumns.map((column) => (
                  <th key={column.name} className="num">
                    {column.name}
                    <div className="tiny muted">{column.unit}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.rows.map((row) => (
                <tr key={row.row}>
                  <td className="num tiny muted">{row.row}</td>
                  <td className="mono">{row.serial || <span className="bad-text">缺序列号</span>}</td>
                  {reagentColumns.map((column) => {
                    const value = row.amounts[column.name];
                    return (
                      <td key={column.name} className={`num mono small${value ? '' : ' muted'}`}>
                        {num(value ?? 0)}
                      </td>
                    );
                  })}
                </tr>
              ))}
              {shown.reagents.length ? (
                <tr>
                  <td />
                  <td className="small">
                    <b>合计</b>
                  </td>
                  {reagentColumns.map((column) => (
                    <td key={column.name} className="num mono small">
                      <b>{num(shown.reagents.find((row) => row.name === column.name)?.total)}</b>
                    </td>
                  ))}
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title={`生成的流程步骤（${shown.steps.length}）`} flush>
        <table>
          <thead>
            <tr>
              <th className="num">序号</th>
              <th>名称</th>
              <th>类型</th>
              <th>投料物料</th>
              <th>依赖</th>
            </tr>
          </thead>
          <tbody>
            {shown.steps.map((step, at) => (
              <tr key={step.step_id ?? at}>
                <td className="num mono">{at + 1}</td>
                <td>
                  {step.name}
                  <div className="tiny muted mono">{step.step_id}</div>
                </td>
                <td className="small">
                  {kindLabel(step)}
                  {kindOf(step) === 'device' && step.cap ? <div className="tiny muted">{capName[step.cap] ?? step.cap}</div> : null}
                </td>
                <td className="small">
                  {step.material ? (
                    <>
                      <span className="tag">{step.material}</span>
                      {step.material_param ? <span className="tiny muted"> · {step.material_param}</span> : null}
                    </>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td className="mono small">
                  {(step.after ?? []).length ? (step.after ?? []).map((id) => position[id] ?? id).join('、') : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </>
  );
}

/* ---------- 导入结果 ---------- */

/** 按流程所处状态给下一步：沿用的流程可能已在评审、已批准或已发布，不能一律让人「提交流程评审」 */
function nextSteps(result: FormulationImportResult): string {
  const state = result.recipe.reused ? result.recipe.state : 'draft';
  if (state === 'review') return '流程评审中，等 QA 批准并发布后锁定并提交方案';
  if (state === 'approved') return '流程已批准，发布后即可锁定并提交方案';
  if (state === 'released') return '流程沿用的是已发布版本，直接锁定并提交方案';
  return '提交流程评审 → QA 批准 → 发布 → 锁定并提交方案';
}

function ResultCard({ result }: { result: FormulationImportResult }) {
  const created = result.samples.filter((row) => row.created).length;
  const recipeState = result.recipe.state_label ?? RECIPE_STATE_LABEL[result.recipe.state] ?? result.recipe.state;
  return (
    <Panel title="导入结果">
      <div className="grid cols-3">
        <div>
          <div className="small muted">实验流程</div>
          <Link to={`/recipes/${result.recipe.id}`}>{result.recipe.name}</Link>
          <div className="tiny muted">
            {result.recipe.reused ? `沿用已有流程（${recipeState}）` : '新建草稿'} · <span className="mono">{result.recipe.id}</span>
          </div>
        </div>
        <div>
          <div className="small muted">实验方案</div>
          <Link to={`/plans/${result.plan.id}`}>{result.plan.name}</Link>
          <div className="tiny muted">
            新建{result.plan.state_label ?? PLAN_STATE_LABEL[result.plan.state] ?? result.plan.state} ·{' '}
            <span className="mono">{result.plan.id}</span>
          </div>
        </div>
        <div>
          <div className="small muted">样本（瓶子）</div>
          <b>{result.samples.length}</b> 个
          <div className="tiny muted">
            新登记 {created} · 沿用 {result.samples.length - created}
          </div>
        </div>
      </div>
      {result.warnings.length ? (
        <div className="note warn">
          <ul className="small">
            {result.warnings.map((text, at) => (
              <li key={at}>{text}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="note">
        接下来：{nextSteps(result)}。方案批准后照常建任务、建批次。
      </div>
    </Panel>
  );
}

/* ---------- 模板规则（只读） ---------- */

function TemplateRules({ template, loading }: { template: FormulationTemplate | undefined; loading: boolean }) {
  if (!template?.config) {
    return (
      <Panel title="模板规则">
        <Empty>{loading ? '加载中…' : '选一个配液模板查看它的生成规则'}</Empty>
      </Panel>
    );
  }
  const config = template.config;
  const stages = config.stages ?? [];
  const routes = Object.entries(config.routes ?? {});
  const names = (rows: { name: string }[] | undefined) => (rows ?? []).map((row) => row.name).join(' → ') || '无';
  /* 阶段的「等哪些步骤完成」存的是固定步骤的局部名（key），给人看的是步骤名称 */
  const fixedName = Object.fromEntries(
    [...(config.prefix ?? []), ...stages.flatMap((stage) => stage.then ?? []), ...(config.suffix ?? [])]
      .filter((row) => row.key)
      .map((row) => [row.key, row.name]),
  );

  return (
    <Panel title="模板规则" aside={<span className="mono">{template.code}</span>}>
      <div>
        <b>{template.name}</b>
        {template.description ? <div className="small muted">{template.description}</div> : null}
        <div className="tiny muted">
          每批样品位 {config.plate}
          {config.sample_type ? ` · 样本类型 ${config.sample_type}` : ''}
          {config.unit ? ` · 表头没写单位按 ${config.unit}` : ''}
        </div>
        {config.serial_headers?.length ? (
          <div className="tiny muted">序列号列表头：{config.serial_headers.join('、')}</div>
        ) : null}
      </div>

      <div>
        <div className="small muted">阶段顺序</div>
        <ol className="small">
          <li>加料前固定步骤：{names(config.prefix)}</li>
          {stages.map((stage) => {
            const categories = routes.filter(([, route]) => route.stage === stage.key).map(([category]) => category);
            return (
              <li key={stage.key}>
                {stage.label}：按表格列顺序逐个加 {categories.join('、') || '（没有类别进这一阶段）'}
                {stage.after?.length ? (
                  <span className="muted">；等「{stage.after.map((key) => fixedName[key] ?? key).join('」「')}」完成后开始</span>
                ) : null}
                <div className="tiny muted">
                  最后一个加料后{stage.stir_after_last === false ? '不' : ''}紧跟搅拌；之后：{names(stage.then)}
                </div>
              </li>
            );
          })}
          <li>加料后固定步骤：{names(config.suffix)}</li>
        </ol>
      </div>

      <table>
        <thead>
          <tr>
            <th>类别</th>
            <th>阶段</th>
            <th>加法</th>
            <th>紧跟搅拌</th>
          </tr>
        </thead>
        <tbody>
          {routes.map(([category, route]) => (
            <tr key={category}>
              <td className="small">{category}</td>
              <td className="small">{stages.find((stage) => stage.key === route.stage)?.label ?? route.stage}</td>
              <td className="small">
                {route.step?.name?.replace('{material}', '…')}
                <div className="tiny muted">
                  用量写到 <span className="mono">{route.param}</span>
                </div>
                {route.not_last ? (
                  <div className="tiny muted">
                    不能是一瓶在本阶段加的最后一种{typeof route.not_last === 'string' ? `：${route.not_last}` : ''}
                  </div>
                ) : null}
              </td>
              <td className="small">{route.stir_after === false ? '否' : '是'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="tiny muted">
        「紧跟搅拌」指不是本阶段最后一个加料时；最后一个按阶段规则。搅拌步骤：{config.stir?.name?.replace('{material}', '…') ?? '未配置'}。
        搅拌按瓶执行：某瓶这种料是 0，这瓶跳过加料和随后的搅拌，「最后一个」也按这瓶自己加的料算。
      </div>

      {config.experiment_params?.length ? (
        <div>
          <div className="small muted">实验参数（每次导入可改）</div>
          <ul className="small">
            {config.experiment_params.map((row) => (
              <li key={row.key}>
                {row.label}：缺省 {row.default} {row.unit}
                <span className="tiny muted mono"> → {row.step}.{row.param}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {config.volume_check ? <VolumeCheckNote config={config} check={config.volume_check} /> : null}
      {config.required_metrics?.length ? (
        <div className="tiny muted">必测指标：{config.required_metrics.join('、')}</div>
      ) : null}
    </Panel>
  );
}

/** 分装量核对的说明：参数写成实验参数的名称，与上面的「实验参数」对得上 */
function VolumeCheckNote({ config, check }: { config: FormulationTemplateConfig; check: FormulationVolumeCheck }) {
  const label = (key: string) => config.experiment_params?.find((row) => row.key === key)?.label ?? key;
  return (
    <div className="tiny muted">
      分装量核对：每瓶总质量 ÷ {check.density} g/mL 估算母液体积，{label(check.bottles)} × {label(check.volume)}
      {check.reserve ? ` + 母瓶留样 ${check.reserve} mL` : ''} 放不下就不能导入
    </div>
  );
}
