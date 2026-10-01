/* 程序表编辑器：充放电工步、升温程序这类「一张表」的参数（能力里登记为程序表的参数）。

   每行一个工步，每列按能力登记的列定义编辑：选项列下拉，数值列填数，或者引用本步另一个数值参数（单位要相同）——
   程序结构固定、要变的量做成本步参数，方案因子按孔位改它，下发前由服务端代进程序表，设备收到的只有具体的数。
   没填的格子就是这一步不用这一列（静置没有电流），标了必填的列每行都要写。能不能下发由服务端判，这里只做即时提示。 */
import { columnSpec, isOptionWindow, isRef, programProblems, type ResolvedSpec, windowFits } from './params';
import type { ProgramCell, ProgramRow } from './types';
import { NumberInput } from './ui';
import { canonicalUnit, withUnit } from './units';

export type ProgramRef = { key: string; label: string; unit: string };

export function ProgramTableEditor({
  spec,
  value,
  onChange,
  refs = [],
  limits,
  readOnly,
}: {
  spec: ResolvedSpec;
  value: ProgramRow[] | undefined;
  onChange: (rows: ProgramRow[]) => void;
  /** 可引用的本步数值参数 */
  refs?: ProgramRef[];
  /** 能承接的工位在各列上的极限（并集），超出的格子标红 */
  limits?: Record<string, [number, number] | string[]>;
  readOnly?: boolean;
}) {
  const rows = value ?? [];
  const problems = programProblems(spec, rows);
  const setCell = (index: number, key: string, cell: ProgramCell | undefined) =>
    onChange(
      rows.map((row, at) => {
        if (at !== index) return row;
        const next = { ...row };
        if (cell === undefined || cell === '') delete next[key];
        else next[key] = cell;
        return next;
      }),
    );
  const move = (index: number, to: number) => {
    if (to < 0 || to >= rows.length) return;
    const next = [...rows];
    const [picked] = next.splice(index, 1);
    next.splice(to, 0, picked);
    onChange(next);
  };

  return (
    <div className="stack program-editor">
      <div className="program-scroll">
        <table>
          <thead>
            <tr>
              <th className="num">#</th>
              {spec.columns.map((column) => (
                <th key={column.key} title={column.key}>
                  {withUnit(column.label || column.key, column.type === 'enum' ? '' : column.unit ?? '')}
                  {column.required ? ' *' : ''}
                </th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                <td className="num mono">{index + 1}</td>
                {spec.columns.map((column) => {
                  const cell = row[column.key];
                  const limit = limits?.[column.key];
                  const out = limit !== undefined && cell !== undefined && !isRef(cell) && !windowFits(cell, limit);
                  if (column.type === 'enum') {
                    return (
                      <td key={column.key}>
                        <select
                          value={typeof cell === 'string' ? cell : ''}
                          disabled={readOnly}
                          className={out ? 'bad' : undefined}
                          aria-label={`第 ${index + 1} 行 ${column.label || column.key}`}
                          onChange={(event) => setCell(index, column.key, event.target.value || undefined)}
                        >
                          <option value="">—</option>
                          {(column.options ?? []).map((option) => (
                            <option key={option} value={option}>
                              {option}
                              {isOptionWindow(limit) && !limit.includes(option) ? '（工位不允许）' : ''}
                            </option>
                          ))}
                        </select>
                      </td>
                    );
                  }
                  const usable = refs.filter((ref) => canonicalUnit(ref.unit) === canonicalUnit(column.unit));
                  return (
                    <td key={column.key}>
                      <div className="row" style={{ gap: 2, flexWrap: 'nowrap' }}>
                        {isRef(cell) ? (
                          <span className="tag" title="下发前代入这个参数的值（按孔位）">
                            = {refs.find((ref) => ref.key === cell.param)?.label ?? cell.param}
                          </span>
                        ) : (
                          <NumberInput
                            value={typeof cell === 'number' ? cell : ''}
                            invalid={out || (cell !== undefined && typeof cell !== 'number')}
                            disabled={readOnly}
                            className="program-cell"
                            ariaLabel={`第 ${index + 1} 行 ${column.label || column.key}`}
                            onChange={(next) => setCell(index, column.key, next === '' ? undefined : next)}
                          />
                        )}
                        {usable.length ? (
                          <select
                            value={isRef(cell) ? cell.param : ''}
                            disabled={readOnly}
                            className="program-ref"
                            title="引用本步的一个数值参数：方案因子按孔位改它，下发前代入"
                            aria-label={`第 ${index + 1} 行 ${column.label || column.key} 引用`}
                            onChange={(event) =>
                              setCell(index, column.key, event.target.value ? { param: event.target.value } : undefined)
                            }
                          >
                            <option value="">填数</option>
                            {usable.map((ref) => (
                              <option key={ref.key} value={ref.key}>
                                = {ref.label}
                              </option>
                            ))}
                          </select>
                        ) : null}
                      </div>
                    </td>
                  );
                })}
                <td className="row-end">
                  {readOnly ? null : (
                    <>
                      <button className="btn sm" aria-label={`第 ${index + 1} 行上移`} disabled={index === 0} onClick={() => move(index, index - 1)}>
                        ↑
                      </button>
                      <button
                        className="btn sm"
                        aria-label={`第 ${index + 1} 行下移`}
                        disabled={index === rows.length - 1}
                        onClick={() => move(index, index + 1)}
                      >
                        ↓
                      </button>
                      <button
                        className="btn sm"
                        disabled={rows.length >= spec.maxRows}
                        onClick={() => onChange([...rows.slice(0, index + 1), { ...row }, ...rows.slice(index + 1)])}
                      >
                        复制
                      </button>
                      <button className="btn sm" onClick={() => onChange(rows.filter((_, at) => at !== index))}>
                        删
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {readOnly ? null : (
        <div className="row">
          <button className="btn sm" disabled={rows.length >= spec.maxRows} onClick={() => onChange([...rows, blankRow(spec)])}>
            加一步
          </button>
          <span className="tiny muted">
            最多 {spec.maxRows} 步；没填的格子是这一步不用这一列；* 为每步必填
          </span>
        </div>
      )}
      {problems.length ? (
        <ul className="issue-list small bad-text">
          {problems.slice(0, 6).map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** 新加的一步：必填的选项列先填第一个选项，其余留空 */
function blankRow(spec: ResolvedSpec): ProgramRow {
  const row: ProgramRow = {};
  spec.columns.forEach((column) => {
    const kind = columnSpec(column);
    if (column.required && kind.type === 'enum' && kind.options.length) row[column.key] = kind.options[0];
  });
  return row;
}
