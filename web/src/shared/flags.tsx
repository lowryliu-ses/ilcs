import type { DataFlag } from './types';

/** 自动打标：越界的值照常入库、质量置可疑；逻辑冲突按规则打标。标记不改值，由复核的人下结论。 */
export function FlagList({ flags }: { flags?: DataFlag[] }) {
  if (!flags?.length) return null;
  return (
    <div>
      {flags.map((flag, index) => (
        <div key={index} className={`tiny ${NOTES.has(flag.code) ? 'muted' : 'warn-text'}`} title={flag.message}>
          {FLAG_LABEL[flag.code] ?? flag.code}：{flag.message}
        </div>
      ))}
    </div>
  );
}

const FLAG_LABEL: Record<string, string> = {
  out_of_range: '越界', logic: '逻辑冲突', output_missing: '缺必报项', output_invalid: '写法不成立',
  // 设备回执自己报的质量不是 good：设备标记这次读数可能无效
  device_quality: '设备质量标记',
  // 来历说明，不是质量问题（不置可疑）：设备回报写成的结果、从曲线派生的数值
  simulated: '模拟示意值', batch_level: '批次级读数', derived: '由曲线派生',
};

/* 只说来历、不是质量问题的标记：灰字显示，不用警示色 */
const NOTES = new Set(['simulated', 'batch_level', 'derived']);
