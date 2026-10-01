/* 驱动配置表单：按驱动目录的字段说明（后端 adapters/catalog.py）出表单，不用手写整份 JSON——

   - 固定键的对象（通道、状态点、启动信号）一格一项；没填的可选项收在「加一项」里，删掉一项就是用驱动缺省；
   - 任意键的表（点表、能力映射、状态映射、故障码）一行一项，键从登记的名字里挑（点名、能力、当前能力的参数、
     参数的选项）；简单的表一列一项，复杂的（能力映射）一项一张卡片；
   - 列表（启动命令、查询）一行一条，可以上下移；
   - 只能取几个值的项是下拉框；引用点名的项提示已登记的点，引用了没登记的点标红，点表上可以一键补登记。

   表单编辑不了的写法（说明里没有的键、和说明对不上的值）原样保留、就地按 JSON 改，不会被表单丢掉；
   完整 JSON 随时可以切过去直接改。加一项能力时照驱动给的起步写法（按能力字典的参数生成）。 */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';

import { api } from '../../shared/api';
import { useQuery } from '../../shared/query';
import type { CapabilityRow, DriverField } from '../../shared/types';
import { NumberInput } from '../../shared/ui';

type Obj = Record<string, unknown>;
type Scope = { capability?: string; param?: string };

/** 表单里引用的名字从哪来：能力与参数取能力字典；给了工位极限就只列工位登记的能力 */
export type FormContext = {
  capabilities: string[];
  params: (capability?: string) => string[];
  options: (capability?: string, param?: string) => string[];
};

type Env = { ctx: FormContext; points: string[]; missingPoints: string[]; readOnly: boolean };

type EditorProps = {
  spec: DriverField;
  value: unknown;
  onChange: (next: unknown) => void;
  env: Env;
  scope: Scope;
  /** 驱动示例配置里对应的那一段：加一项时照它起步 */
  example?: unknown;
  depth: number;
};

const SIMPLE = new Set<DriverField['type']>(['string', 'integer', 'number', 'boolean', 'scalar']);

const isObj = (value: unknown): value is Obj => !!value && typeof value === 'object' && !Array.isArray(value);
const isScalar = (value: unknown): value is string | number | boolean =>
  typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean';
const unique = (values: string[]) => [...new Set(values.filter(Boolean))];
const simple = (spec: DriverField) => SIMPLE.has(spec.type);
/** 只有简单项的对象：在表里一列一项 */
const flat = (spec: DriverField) => spec.type === 'object' && !!spec.fields?.length && spec.fields.every(simple);
const clone = <T,>(value: T): T => (value === undefined ? value : JSON.parse(JSON.stringify(value)));
const child = (example: unknown, key: string | number): unknown =>
  Array.isArray(example) ? example[Number(key)] : isObj(example) ? example[String(key)] : undefined;
/** 表里、列表里的一项清空后留个空值占位，不让整行消失 */
const emptyOf = (spec: DriverField) => (spec.type === 'string' || spec.type === 'scalar' ? '' : null);

function fits(spec: DriverField, value: unknown): boolean {
  if (value === undefined || value === null) return true;
  switch (spec.type) {
    case 'string': return typeof value === 'string';
    case 'integer': return typeof value === 'number' && Number.isInteger(value);
    case 'number': return typeof value === 'number';
    case 'boolean': return typeof value === 'boolean';
    case 'scalar': return isScalar(value);
    case 'any': return true;
    case 'object': return isObj(value) || (!!spec.shorthand && isScalar(value) && typeof value !== 'boolean');
    case 'array': return Array.isArray(value) || (!!spec.single && isObj(value));
    default: return false;
  }
}

/** 简写展开成对象编辑；改完只剩简写那一项的写回简写（驱动两种都认） */
function expand(spec: DriverField, value: unknown): Obj {
  if (isObj(value)) return value;
  if (spec.shorthand && isScalar(value)) return { [spec.shorthand]: value };
  return {};
}

function compact(spec: DriverField, value: Obj): unknown {
  const keys = Object.keys(value);
  if (spec.shorthand && keys.length === 1 && keys[0] === spec.shorthand && isScalar(value[spec.shorthand])) {
    return value[spec.shorthand];
  }
  return value;
}

/** 新加一项的起步值：驱动示例里有就照抄，没有按类型给空值（对象只带必填项） */
function blank(spec: DriverField, example?: unknown): unknown {
  if (example !== undefined && example !== null && fits(spec, example)) return clone(example);
  switch (spec.type) {
    case 'string': return spec.options?.[0] ?? '';
    case 'integer':
    case 'number': return 0;
    case 'boolean': return false;
    case 'scalar': return '';
    case 'array': return [];
    case 'any': return {};
    case 'object':
      if (spec.shorthand) return '';
      return Object.fromEntries((spec.fields ?? []).filter((field) => field.required).map((field) => [field.name, blank(field)]));
    default: return '';
  }
}

function names(spec: DriverField, env: Env, scope: Scope, kind: 'ref' | 'key'): string[] {
  const source = kind === 'ref' ? spec.ref : spec.key_ref;
  const extra = kind === 'key' ? spec.key_options ?? [] : [];
  switch (source) {
    case 'points': return unique([...env.points, ...extra]);
    case 'capabilities': return unique([...env.ctx.capabilities, ...extra]);
    case 'params': return unique([...env.ctx.params(scope.capability), ...extra]);
    case 'options': return unique([...env.ctx.options(scope.capability, scope.param), ...extra]);
    default: return extra;
  }
}

/** 配置里引用到的点名（值引用点名、以点名为键的表），用来标出还没登记的点 */
function referencedPoints(spec: DriverField, value: unknown, out: Set<string>): void {
  if (value === undefined || value === null) return;
  if (spec.ref === 'points' && typeof value === 'string' && value) out.add(value);
  if (spec.type === 'object') {
    if (spec.shorthand && isScalar(value)) {
      const field = spec.fields?.find((item) => item.name === spec.shorthand);
      if (field) referencedPoints(field, value, out);
      return;
    }
    if (!isObj(value)) return;
    if (spec.fields?.length) {
      for (const field of spec.fields) referencedPoints(field, value[field.name], out);
    } else if (spec.entries) {
      for (const [key, item] of Object.entries(value)) {
        if (spec.key_ref === 'points') out.add(key);
        referencedPoints(spec.entries, item, out);
      }
    }
  } else if (spec.type === 'array' && spec.items) {
    const list = Array.isArray(value) ? value : spec.single && isObj(value) ? [value] : [];
    for (const item of list) referencedPoints(spec.items, item, out);
  }
}

function countOf(value: unknown): string {
  if (Array.isArray(value)) return `${value.length} 条`;
  if (isObj(value)) return `${Object.keys(value).length} 项`;
  return '';
}

/** 能力字典 + 工位极限 → 表单里可挑的能力、参数与选项 */
export function useFormContext(limits?: Record<string, Record<string, unknown>>): FormContext {
  const capabilities = useQuery<CapabilityRow[]>('capabilities', () => api.get<CapabilityRow[]>('/capabilities'));
  return useMemo(() => {
    const rows = (capabilities.data ?? []).filter((row) => !row.retired);
    const byId = new Map(rows.map((row) => [row.id, row]));
    const ids = limits ? Object.keys(limits) : rows.map((row) => row.id);
    const paramsOf = (capability?: string): string[] => {
      if (!capability) return unique(ids.flatMap((id) => paramsOf(id)));
      return unique([...Object.keys(limits?.[capability] ?? {}), ...Object.keys(byId.get(capability)?.params ?? {})]);
    };
    const optionsOf = (capability?: string, param?: string): string[] => {
      if (!param) return [];
      const specs = capability ? [byId.get(capability)?.param_specs?.[param]] : rows.map((row) => row.param_specs?.[param]);
      return unique(specs.flatMap((spec) => spec?.options ?? []));
    };
    return { capabilities: ids, params: paramsOf, options: optionsOf };
  }, [capabilities.data, limits]);
}

// ---------- 值 ----------

function ValueEditor(props: EditorProps) {
  const { spec, value, onChange, env } = props;
  const nested = !!(spec.fields?.length || spec.entries || spec.items);
  if (!fits(spec, value)) {
    return <JsonValue value={value} onChange={onChange} readOnly={env.readOnly} note={`写法和说明（${spec.type_label}）对不上，按 JSON 改`} />;
  }
  if (spec.type === 'any' || ((spec.type === 'object' || spec.type === 'array') && !nested)) {
    return <JsonValue value={value} onChange={onChange} readOnly={env.readOnly} />;
  }
  if (spec.type === 'object' && spec.fields?.length) return <RecordEditor {...props} />;
  if (spec.type === 'object') return <TableEditor {...props} />;
  if (spec.type === 'array') return <ListEditor {...props} />;
  return <ScalarInput {...props} />;
}

function ScalarInput({ spec, value, onChange, env, scope }: EditorProps) {
  const listId = useId();
  if (spec.type === 'boolean') {
    return (
      <select value={value === true ? 'true' : value === false ? 'false' : ''} disabled={env.readOnly} aria-label={spec.label}
        onChange={(event) => onChange(event.target.value === '' ? undefined : event.target.value === 'true')}>
        <option value="">（缺省）</option>
        <option value="true">是</option>
        <option value="false">否</option>
      </select>
    );
  }
  if (spec.options?.length) {
    const current = value === undefined || value === null ? '' : String(value);
    return (
      <select value={current} disabled={env.readOnly} aria-label={spec.label}
        onChange={(event) => onChange(event.target.value === '' ? undefined : event.target.value)}>
        <option value="">{spec.required ? '选择…' : '（缺省）'}</option>
        {current && !spec.options.includes(current) ? <option value={current}>{current}（不在可选值里）</option> : null}
        {spec.options.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    );
  }
  if (spec.type === 'integer' || spec.type === 'number') {
    return (
      <NumberInput value={typeof value === 'number' ? value : ''} disabled={env.readOnly} ariaLabel={spec.label}
        invalid={spec.type === 'integer' && typeof value === 'number' && !Number.isInteger(value)}
        onChange={(next) => onChange(next === '' ? undefined : next)} />
    );
  }
  const suggestions = names(spec, env, scope, 'ref');
  const unregistered = spec.ref === 'points' && typeof value === 'string' && value !== '' && !env.points.includes(value);
  return (
    <>
      <TextValue value={value} scalar={spec.type === 'scalar'} readOnly={env.readOnly} list={suggestions.length ? listId : undefined}
        invalid={unregistered} title={unregistered ? `点 ${value} 没有在点表里登记` : undefined} ariaLabel={spec.label}
        onChange={onChange} />
      {suggestions.length ? <datalist id={listId}>{suggestions.map((name) => <option key={name} value={name} />)}</datalist> : null}
    </>
  );
}

/** scalar：true / false / 数字按值写入，其余按文本（要写成文本的数字请切到 JSON） */
function parseScalar(text: string): unknown {
  const trimmed = text.trim();
  if (trimmed === 'true') return true;
  if (trimmed === 'false') return false;
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  return text;
}

function TextValue({
  value, onChange, scalar, readOnly, list, invalid, title, ariaLabel,
}: {
  value: unknown;
  onChange: (next: unknown) => void;
  scalar: boolean;
  readOnly: boolean;
  list?: string;
  invalid?: boolean;
  title?: string;
  ariaLabel?: string;
}) {
  const [text, setText] = useState(value === undefined || value === null ? '' : String(value));
  const committed = useRef<unknown>(value);
  useEffect(() => {
    if (value !== committed.current) {
      committed.current = value;
      setText(value === undefined || value === null ? '' : String(value));
    }
  }, [value]);
  return (
    <input
      className={['mono', invalid ? 'bad' : ''].filter(Boolean).join(' ')}
      value={text}
      readOnly={readOnly}
      list={list}
      title={title}
      aria-label={ariaLabel}
      onChange={(event) => {
        const raw = event.target.value;
        setText(raw);
        const next = raw === '' ? undefined : scalar ? parseScalar(raw) : raw;
        committed.current = next;
        onChange(next);
      }}
    />
  );
}

/** 表单编辑不了的一段：原样保留，按 JSON 改；写错时不往外传 */
function JsonValue({ value, onChange, readOnly, note }: { value: unknown; onChange: (next: unknown) => void; readOnly: boolean; note?: string }) {
  const shown = value === undefined ? '' : JSON.stringify(value, null, 2);
  const [text, setText] = useState(shown);
  const [error, setError] = useState('');
  const committed = useRef(shown);
  useEffect(() => {
    if (shown !== committed.current) {
      committed.current = shown;
      setText(shown);
      setError('');
    }
  }, [shown]);
  return (
    <div className="cfg-json">
      {note ? <span className="cfg-hint warn-text">{note}</span> : null}
      <textarea
        className={`mono${error ? ' bad' : ''}`}
        rows={Math.min(12, Math.max(1, text.split('\n').length))}
        value={text}
        readOnly={readOnly}
        onChange={(event) => {
          const raw = event.target.value;
          setText(raw);
          if (raw.trim() === '') {
            setError('');
            committed.current = '';
            onChange(undefined);
            return;
          }
          try {
            const parsed = JSON.parse(raw);
            setError('');
            committed.current = JSON.stringify(parsed, null, 2);
            onChange(parsed);
          } catch (caught) {
            setError(caught instanceof Error ? caught.message : 'JSON 无效');
          }
        }}
      />
      {error ? <span className="cfg-hint bad-text">JSON 无效，没有生效：{error}</span> : null}
    </div>
  );
}

// ---------- 对象：固定的几个键 ----------

function RecordEditor({
  spec, value, onChange, env, scope, example, depth, showAll = false, known, elsewhere,
}: EditorProps & { showAll?: boolean; known?: DriverField[]; elsewhere?: (name: string) => string }) {
  const record = expand(spec, value);
  const fields = spec.fields ?? [];
  const own = new Set(fields.map((field) => field.name));
  const other = new Map((known ?? []).filter((field) => !own.has(field.name)).map((field) => [field.name, field]));
  // 带「能力」的对象（验收缺省）：下面的参数表按这项能力提示参数
  const scoped = fields.some((field) => field.name === 'capability' && field.ref === 'capabilities')
    && typeof record.capability === 'string' && record.capability ? { ...scope, capability: record.capability } : scope;
  const shown = fields.filter((field) => showAll || field.required || field.name in record);
  const addable = fields.filter((field) => !showAll && !field.required && !(field.name in record));
  const extra = Object.keys(record).filter((key) => !own.has(key));
  const set = (name: string, next: unknown) => {
    const copy: Obj = { ...record };
    if (next === undefined) delete copy[name];
    else copy[name] = next;
    onChange(compact(spec, copy));
  };
  const removable = (field: DriverField) => !field.required && !showAll && !env.readOnly && field.name in record;
  const plain = shown.filter(simple);
  const nested = shown.filter((field) => !simple(field));
  return (
    <div className="cfg-record">
      {plain.length ? (
        <div className="cfg-grid">
          {plain.map((field) => (
            <div key={field.name} className="cfg-field">
              <span className="cfg-label">
                {field.label}
                {field.required ? <b className="cfg-required" title="必填">*</b> : null}
                <span className="cfg-key mono">{field.name}</span>
                {removable(field) ? (
                  <button type="button" className="cfg-x" title="删掉这一项（用驱动缺省）" onClick={() => set(field.name, undefined)}>×</button>
                ) : null}
              </span>
              <ValueEditor spec={field} value={record[field.name]} onChange={(next) => set(field.name, next)} env={env}
                scope={scoped} example={child(example, field.name)} depth={depth + 1} />
              {field.hint ? <span className="cfg-hint">{field.hint}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
      {nested.map((field) => (
        <Section key={field.name} spec={field} value={record[field.name]} depth={depth}
          onRemove={removable(field) ? () => set(field.name, undefined) : undefined}>
          <ValueEditor spec={field} value={record[field.name]} onChange={(next) => set(field.name, next)} env={env}
            scope={scoped} example={child(example, field.name)} depth={depth + 1} />
        </Section>
      ))}
      {extra.map((key) => {
        const field = other.get(key);
        return (
          <div key={key} className="cfg-extra">
            <span className="cfg-label">
              <span className="tag warn">{field ? '不在这里填' : '未登记'}</span>
              <span className="mono">{key}</span>
              <span className="cfg-hint">
                {field ? `${field.label}：${elsewhere?.(key) || '不在这张表单里'}` : '不是登记的配置项：拼错的键会被驱动忽略，请核对'}
              </span>
              {env.readOnly ? null : <button type="button" className="cfg-x" title="删掉这个键" onClick={() => set(key, undefined)}>×</button>}
            </span>
            <JsonValue value={record[key]} onChange={(next) => set(key, next)} readOnly={env.readOnly} />
          </div>
        );
      })}
      {addable.length && !env.readOnly ? (
        <select className="cfg-add" value="" aria-label="加一项配置"
          onChange={(event) => {
            const field = fields.find((item) => item.name === event.target.value);
            if (field) set(field.name, blank(field, child(example, field.name)));
          }}>
          <option value="">＋ 加一项…</option>
          {addable.map((field) => <option key={field.name} value={field.name}>{field.label}（{field.name}）</option>)}
        </select>
      ) : null}
    </div>
  );
}

function Section({ spec, value, depth, onRemove, children }: {
  spec: DriverField; value: unknown; depth: number; onRemove?: () => void; children: ReactNode;
}) {
  const [open, setOpen] = useState(depth > 0 || spec.required);
  return (
    <div className={`cfg-section${open ? ' open' : ''}`}>
      <div className="cfg-section-head">
        <button type="button" className="cfg-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? '▾' : '▸'} <b>{spec.label}</b>
          {spec.required ? <b className="cfg-required">*</b> : null}
          <span className="cfg-key mono">{spec.name}</span>
          {value !== undefined ? <span className="cfg-hint">{countOf(value)}</span> : null}
        </button>
        {spec.hint ? <span className="cfg-hint">{spec.hint}</span> : null}
        {onRemove ? <button type="button" className="cfg-x" title="删掉这一项（用驱动缺省）" onClick={onRemove}>×</button> : null}
      </div>
      {open ? <div className="cfg-section-body">{children}</div> : null}
    </div>
  );
}

// ---------- 对象：任意键 → 同一种值 ----------

function KeyInput({ value, suggestions, readOnly, onRename, label }: {
  value: string; suggestions: string[]; readOnly: boolean; onRename: (to: string) => boolean; label: string;
}) {
  const listId = useId();
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const commit = () => {
    if (text !== value && !onRename(text)) setText(value);
  };
  return (
    <>
      <input className="mono cfg-keyinput" value={text} readOnly={readOnly} aria-label={label} list={suggestions.length ? listId : undefined}
        title="改名：离开输入框时生效；不能和已有的重名"
        onChange={(event) => setText(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            commit();
          }
        }} />
      {suggestions.length ? <datalist id={listId}>{suggestions.map((name) => <option key={name} value={name} />)}</datalist> : null}
    </>
  );
}

function TableEditor({ spec, value, onChange, env, scope, example, depth }: EditorProps) {
  const table = isObj(value) ? value : {};
  const entry = spec.entries as DriverField;
  const keys = Object.keys(table);
  const suggestions = names(spec, env, scope, 'key');
  const unused = suggestions.filter((name) => !(name in table));
  const keyLabel = spec.key_label || '键';
  const [wide, setWide] = useState(false);
  const [opened, setOpened] = useState<Set<string>>(() => new Set(keys.length <= 2 ? keys : []));
  const scopeOf = (key: string): Scope =>
    spec.scope === 'capability' ? { ...scope, capability: key } : spec.scope === 'param' ? { ...scope, param: key } : scope;
  const setValue = (key: string, next: unknown) => onChange({ ...table, [key]: next === undefined ? emptyOf(entry) : next });
  const remove = (key: string) => {
    const copy = { ...table };
    delete copy[key];
    onChange(copy);
  };
  const rename = (from: string, to: string) => {
    const name = to.trim();
    if (!name || name === from || name in table) return false;
    onChange(Object.fromEntries(Object.entries(table).map(([key, item]) => [key === from ? name : key, item])));
    return true;
  };
  const add = (wanted: string) => {
    let key = wanted;
    if (!key) {
      let index = 1;
      while (`${keyLabel}${index}` in table) index += 1;
      key = `${keyLabel}${index}`;
    }
    onChange({ ...table, [key]: blank(entry, child(example, key)) });
    setOpened((current) => new Set([...current, key]));
  };
  // 点表：配置里引用了、还没登记的点，一键补上
  const missing = spec.name === 'points' ? env.missingPoints : [];
  const adder = env.readOnly ? null : (
    <div className="row cfg-adder">
      <select value="" aria-label={`加一个${keyLabel}`} onChange={(event) => {
        if (event.target.value) add(event.target.value === '*' ? '' : event.target.value);
      }}>
        <option value="">＋ 加一个{keyLabel}…</option>
        {unused.map((name) => <option key={name} value={name}>{name}</option>)}
        <option value="*">（自己起名）</option>
      </select>
      {missing.length ? (
        <button type="button" className="btn sm" title={missing.join('、')}
          onClick={() => onChange({ ...table, ...Object.fromEntries(missing.map((name) => [name, blank(entry)])) })}>
          补登记引用了的 {missing.length} 个点
        </button>
      ) : null}
    </div>
  );

  // 一行一项：值是简单项、只有简单项的对象，或能简写成一个简单项的对象（设定值 = 点名，另可带选项代码表）；
  // 后者的复杂项（选项代码表）在行下面展开编辑
  const tabular = simple(entry) || flat(entry)
    || (!!entry.shorthand && !!entry.fields?.some((field) => field.name === entry.shorthand && simple(field)));
  if (tabular) {
    const plain = simple(entry) ? [entry] : (entry.fields ?? []).filter(simple);
    const columns = simple(entry)
      ? [entry]
      : plain.filter((field) => wide || field.required || field.name === entry.shorthand
        || keys.some((key) => field.name in expand(entry, table[key])));
    const nested = simple(entry) ? [] : (entry.fields ?? []).filter((field) => !simple(field));
    const hidden = plain.length - columns.length;
    const span = columns.length + (nested.length ? 3 : 2);
    const toggle = (id: string) => setOpened((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    const setField = (key: string, record: Obj, name: string, next: unknown) => {
      const copy = { ...record };
      if (next === undefined) delete copy[name];
      else copy[name] = next;
      setValue(key, compact(entry, copy));
    };
    return (
      <div className="cfg-table-wrap">
        {keys.length ? (
          <div className="program-scroll">
            <table className="compact cfg-table">
              <thead>
                <tr>
                  <th>{keyLabel}</th>
                  {columns.map((field) => (
                    <th key={field.name || 'value'} title={field.hint || undefined}>
                      {field.label}{field.required && !simple(entry) ? ' *' : ''}
                    </th>
                  ))}
                  {nested.length ? <th /> : null}
                  <th />
                </tr>
              </thead>
              <tbody>
                {keys.flatMap((key) => {
                  const row = table[key];
                  const record = expand(entry, row);
                  const rows = [
                    <tr key={key}>
                      <td><KeyInput value={key} suggestions={suggestions} readOnly={env.readOnly} label={keyLabel} onRename={(to) => rename(key, to)} /></td>
                      {!fits(entry, row) ? (
                        <td colSpan={columns.length + (nested.length ? 1 : 0)}>
                          <JsonValue value={row} onChange={(next) => setValue(key, next)} readOnly={env.readOnly} note="写法和说明对不上，按 JSON 改" />
                        </td>
                      ) : simple(entry) ? (
                        <td><ValueEditor spec={entry} value={row} onChange={(next) => setValue(key, next)} env={env} scope={scopeOf(key)} depth={depth + 1} /></td>
                      ) : (
                        <>
                          {columns.map((field) => (
                            <td key={field.name}>
                              <ValueEditor spec={field} value={record[field.name]} env={env} scope={scopeOf(key)} depth={depth + 1}
                                onChange={(next) => setField(key, record, field.name, next)} />
                            </td>
                          ))}
                          {nested.length ? (
                            <td className="cfg-tools">
                              {nested.map((field) => (
                                <button key={field.name} type="button" className="btn sm" title={field.hint || undefined}
                                  aria-expanded={opened.has(`${key}\u0000${field.name}`)} onClick={() => toggle(`${key}\u0000${field.name}`)}>
                                  {field.label}{field.name in record ? `（${countOf(record[field.name])}）` : ' ＋'}
                                </button>
                              ))}
                            </td>
                          ) : null}
                        </>
                      )}
                      <td className="row-end">
                        {env.readOnly ? null : <button type="button" className="btn sm" onClick={() => remove(key)}>删</button>}
                      </td>
                    </tr>,
                  ];
                  if (fits(entry, row)) {
                    for (const field of nested) {
                      if (!opened.has(`${key}\u0000${field.name}`)) continue;
                      rows.push(
                        <tr key={`${key}\u0000${field.name}`} className="cfg-subrow">
                          <td colSpan={span}>
                            <div className="cfg-section-head">
                              <b className="small">{key} · {field.label}</b>
                              {field.hint ? <span className="cfg-hint">{field.hint}</span> : null}
                              {field.name in record && !env.readOnly ? (
                                <button type="button" className="cfg-x" title="删掉这一项" onClick={() => setField(key, record, field.name, undefined)}>×</button>
                              ) : null}
                            </div>
                            <ValueEditor spec={field} value={record[field.name]} env={env} scope={scopeOf(key)} depth={depth + 1}
                              example={child(child(example, key), field.name)} onChange={(next) => setField(key, record, field.name, next)} />
                          </td>
                        </tr>,
                      );
                    }
                  }
                  return rows;
                })}
              </tbody>
            </table>
          </div>
        ) : <div className="small muted">还没有{keyLabel}</div>}
        <div className="row">
          {adder}
          {hidden > 0 ? <button type="button" className="btn sm" onClick={() => setWide(true)}>显示全部列（还有 {hidden} 列）</button> : null}
        </div>
      </div>
    );
  }

  return (
    <div className="cfg-cards">
      {keys.length ? null : <div className="small muted">还没有{keyLabel}</div>}
      {keys.map((key) => {
        const open = opened.has(key);
        return (
          <div key={key} className="cfg-card">
            <div className="cfg-card-head">
              <button type="button" className="cfg-toggle" aria-expanded={open} title={open ? '收起' : '展开'}
                onClick={() => setOpened((current) => {
                  const next = new Set(current);
                  if (next.has(key)) next.delete(key);
                  else next.add(key);
                  return next;
                })}>
                {open ? '▾' : '▸'}
              </button>
              <span className="cfg-hint">{keyLabel}</span>
              <KeyInput value={key} suggestions={suggestions} readOnly={env.readOnly} label={keyLabel} onRename={(to) => rename(key, to)} />
              <span className="cfg-hint">{countOf(table[key])}</span>
              <span style={{ flex: 1 }} />
              {env.readOnly ? null : <button type="button" className="btn sm" onClick={() => remove(key)}>删</button>}
            </div>
            {open ? (
              <div className="cfg-card-body">
                <ValueEditor spec={entry} value={table[key]} onChange={(next) => setValue(key, next)} env={env} scope={scopeOf(key)}
                  example={child(example, key)} depth={depth + 1} />
              </div>
            ) : null}
          </div>
        );
      })}
      {adder}
    </div>
  );
}

// ---------- 列表 ----------

function ListEditor({ spec, value, onChange, env, scope, example, depth }: EditorProps) {
  const item = spec.items as DriverField;
  const single = !!spec.single && isObj(value);
  const list: unknown[] = single ? [value] : Array.isArray(value) ? value : [];
  const emit = (next: unknown[]) => onChange(single && next.length === 1 ? next[0] : next);
  const set = (index: number, next: unknown) => emit(list.map((row, at) => (at === index ? (next === undefined ? emptyOf(item) : next) : row)));
  const remove = (index: number) => emit(list.filter((_, at) => at !== index));
  const move = (index: number, to: number) => {
    if (to < 0 || to >= list.length) return;
    const next = [...list];
    const [picked] = next.splice(index, 1);
    next.splice(to, 0, picked);
    emit(next);
  };
  const add = () => emit([...list, blank(item, child(example, list.length) ?? child(example, 0))]);
  const tools = (index: number) => env.readOnly ? null : (
    <span className="cfg-tools">
      <button type="button" className="btn sm" disabled={index === 0} onClick={() => move(index, index - 1)}>↑</button>
      <button type="button" className="btn sm" disabled={index === list.length - 1} onClick={() => move(index, index + 1)}>↓</button>
      <button type="button" className="btn sm" onClick={() => remove(index)}>删</button>
    </span>
  );

  if (simple(item)) {
    return (
      <div className="cfg-chips">
        {list.map((row, index) => (
          <span key={index} className="cfg-chip">
            <ValueEditor spec={item} value={row} onChange={(next) => set(index, next)} env={env} scope={scope} depth={depth + 1} />
            {env.readOnly ? null : <button type="button" className="cfg-x" title="删掉这一个" onClick={() => remove(index)}>×</button>}
          </span>
        ))}
        {env.readOnly ? null : <button type="button" className="btn sm" onClick={add}>＋ {item.label}</button>}
        {!list.length && env.readOnly ? <span className="small muted">（空）</span> : null}
      </div>
    );
  }

  if (flat(item)) {
    const columns = (item.fields ?? []).filter((field) => field.required || list.some((row) => field.name in expand(item, row)));
    const addable = (item.fields ?? []).filter((field) => !columns.includes(field));
    return (
      <div className="cfg-table-wrap">
        {list.length ? (
          <div className="program-scroll">
            <table className="compact cfg-table">
              <thead>
                <tr>
                  <th>#</th>
                  {columns.map((field) => <th key={field.name} title={field.hint || undefined}>{field.label}{field.required ? ' *' : ''}</th>)}
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.map((row, index) => {
                  const record = expand(item, row);
                  return (
                    <tr key={index}>
                      <td className="small muted">{index + 1}</td>
                      {!fits(item, row) ? (
                        <td colSpan={columns.length}>
                          <JsonValue value={row} onChange={(next) => set(index, next)} readOnly={env.readOnly} note="写法和说明对不上，按 JSON 改" />
                        </td>
                      ) : columns.map((field) => (
                        <td key={field.name}>
                          <ValueEditor spec={field} value={record[field.name]} env={env} scope={scope} depth={depth + 1}
                            onChange={(next) => {
                              const copy = { ...record };
                              if (next === undefined) delete copy[field.name];
                              else copy[field.name] = next;
                              set(index, compact(item, copy));
                            }} />
                        </td>
                      ))}
                      <td className="row-end">{tools(index)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : <div className="small muted">还没有{item.label}</div>}
        {env.readOnly ? null : (
          <div className="row">
            <button type="button" className="btn sm" onClick={add}>＋ 加一条{item.label}</button>
            {addable.length && list.length ? (
              <select value="" aria-label="加一列" onChange={(event) => {
                const field = addable.find((row) => row.name === event.target.value);
                if (!field) return;
                // 加一列：先在第一条上写个空值，这一列就出现了
                set(0, compact(item, { ...expand(item, list[0]), [field.name]: blank(field) }));
              }}>
                <option value="">＋ 加一列…</option>
                {addable.map((field) => <option key={field.name} value={field.name}>{field.label}（{field.name}）</option>)}
              </select>
            ) : null}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="cfg-cards">
      {list.map((row, index) => (
        <div key={index} className="cfg-card">
          <div className="cfg-card-head">
            <span className="cfg-hint">第 {index + 1} 条</span>
            <span style={{ flex: 1 }} />
            {tools(index)}
          </div>
          <div className="cfg-card-body">
            <ValueEditor spec={item} value={row} onChange={(next) => set(index, next)} env={env} scope={scope}
              example={child(example, index) ?? child(example, 0)} depth={depth + 1} />
          </div>
        </div>
      ))}
      {env.readOnly ? null : <button type="button" className="btn sm" onClick={add}>＋ 加一条{item.label}</button>}
    </div>
  );
}

// ---------- 入口 ----------

export function ConfigForm({
  fields, known, value, onChange, readOnly = false, context, example, showAll = false, elsewhere,
}: {
  fields: DriverField[];
  /** 驱动的全部配置项：不在 `fields` 里、却出现在配置里的，按 `elsewhere` 说明该写到哪 */
  known?: DriverField[];
  value: Obj;
  onChange: (next: Obj) => void;
  readOnly?: boolean;
  context: FormContext;
  example?: Obj;
  /** 把 `fields` 全部摆出来（连接参数这种短表）；缺省只摆已填的与必填的 */
  showAll?: boolean;
  elsewhere?: (name: string) => string;
}) {
  const root = useMemo<DriverField>(
    () => ({ name: '', label: '', type: 'object', type_label: '对象', required: true, connection: false, hint: '', fields }),
    [fields],
  );
  const env = useMemo<Env>(() => {
    const points = isObj(value.points) ? Object.keys(value.points) : [];
    const used = new Set<string>();
    referencedPoints(root, value, used);
    return { ctx: context, points, missingPoints: points.length || 'points' in value ? [...used].filter((name) => !points.includes(name)) : [], readOnly };
  }, [context, readOnly, root, value]);
  return (
    <RecordEditor spec={root} value={value} onChange={(next) => onChange(isObj(next) ? next : {})} env={env} scope={{}}
      example={example} depth={0} showAll={showAll} known={known} elsewhere={elsewhere} />
  );
}

/** 表单 / 完整 JSON 两种写法切换。JSON 写错时不往外传，`onInvalid` 告诉外面现在不能保存。 */
export function ConfigEditor({
  label, hint, onInvalid, rows = 12, jsonOnly = false, ...form
}: Parameters<typeof ConfigForm>[0] & {
  label: string;
  hint?: string;
  onInvalid?: (message: string) => void;
  rows?: number;
  /** 没有配置项说明（内置模拟）：只按 JSON 写 */
  jsonOnly?: boolean;
}) {
  const [mode, setMode] = useState<'form' | 'json'>(jsonOnly ? 'json' : 'form');
  const [text, setText] = useState(() => (jsonOnly ? JSON.stringify(form.value ?? {}, null, 2) : ''));
  const [error, setError] = useState('');
  // 外面换了整份配置（从驱动示例开始、读到已保存的配置），JSON 视图跟着换
  const shown = useRef(form.value);
  useEffect(() => {
    if (mode === 'json' && form.value !== shown.current) {
      shown.current = form.value;
      setText(JSON.stringify(form.value ?? {}, null, 2));
      setError('');
      onInvalid?.('');
    }
  }, [form.value, mode, onInvalid]);
  // 内置模拟只能按 JSON 写；换成真实驱动（有了配置项说明）就回到表单
  const wasJsonOnly = useRef(jsonOnly);
  useEffect(() => {
    if (jsonOnly && mode !== 'json') {
      setText(JSON.stringify(form.value ?? {}, null, 2));
      setMode('json');
    } else if (!jsonOnly && wasJsonOnly.current && !error) {
      setMode('form');
    }
    wasJsonOnly.current = jsonOnly;
  }, [jsonOnly, mode, form.value, error]);
  return (
    <div className="cfg-editor">
      <div className="subsection-head">
        <span>
          <b className="small">{label}</b>
          {hint ? <span className="small muted"> · {hint}</span> : null}
        </span>
        <div className="seg" hidden={jsonOnly}>
          <button type="button" className={mode === 'form' ? 'on' : ''} disabled={!!error}
            title={error ? 'JSON 改好之前不能切回表单' : undefined} onClick={() => setMode('form')}>
            表单
          </button>
          <button type="button" className={mode === 'json' ? 'on' : ''}
            onClick={() => {
              if (mode === 'json') return;
              shown.current = form.value;
              setText(JSON.stringify(form.value ?? {}, null, 2));
              setError('');
              setMode('json');
            }}>
            JSON
          </button>
        </div>
      </div>
      {mode === 'form' ? (
        <ConfigForm {...form} />
      ) : (
        <>
          <textarea className={`mono${error ? ' bad' : ''}`} rows={rows} value={text} readOnly={form.readOnly}
            onChange={(event) => {
              const raw = event.target.value;
              setText(raw);
              try {
                const parsed = JSON.parse(raw || '{}');
                if (!isObj(parsed)) throw new Error('必须是 JSON 对象');
                setError('');
                onInvalid?.('');
                shown.current = parsed;
                form.onChange(parsed);
              } catch (caught) {
                const message = caught instanceof Error ? caught.message : 'JSON 无效';
                setError(message);
                onInvalid?.(`${label}：JSON 无效（${message}）`);
              }
            }} />
          {error ? <div className="small bad-text">JSON 无效：{error}。改好之前不能切回表单，也不能保存。</div> : null}
        </>
      )}
    </div>
  );
}
