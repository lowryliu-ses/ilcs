import type { DataFlag } from './types';

/** 自动打标：越界的值照常入库、质量置可疑；逻辑冲突按规则打标。标记不改值，由复核的人下结论。 */
export function FlagList({ flags }: { flags?: DataFlag[] }) {
  if (!flags?.length) return null;
  return (
    <div>
      {flags.map((flag, index) => (
        <div key={index} className="tiny warn-text" title={flag.message}>
          {FLAG_LABEL[flag.code] ?? flag.code}：{flag.message}
        </div>
      ))}
    </div>
  );
}

const FLAG_LABEL: Record<string, string> = {
  out_of_range: '越界', logic: '逻辑冲突', output_missing: '缺必报项', output_invalid: '非数值',
};
