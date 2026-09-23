# ILCS 历史数据迁移报告

组织归属：ORG-001

- 创建组织 ORG-001（本部电池实验室）
- alarms 归属 ORG-001：3 行
- analysis_tasks 归属 ORG-001：8 行
- batches 归属 ORG-001：1 行
- commands 归属 ORG-001：4 行
- lots 归属 ORG-001：6 行
- plans 归属 ORG-001：3 行
- recipes 归属 ORG-001：4 行
- reservations 归属 ORG-001：1 行
- results 归属 ORG-001：8 行
- samples 归属 ORG-001：8 行
- stations 归属 ORG-001：10 行
- waste_tanks 归属 ORG-001：3 行
- audit_events 归属 ORG-001：24 行
- 组织成员关系：5 个账号加入 ORG-001
- 物料主数据：按（名称，单位）建 5 条，旧名称保留展示
- 库存期初：6 个批号写入 opening 流水；未按步骤比例倒扣任何历史消耗
- 未结预留 0 条、已标记消耗 1 条保持原样；旧 delivered_qty 为按步骤比例推算值，未迁入 consumed_qty，需责任人确认（DEC-05）
- 物理样本：8 条按原 Sample ID 建立，运行分配指向同一 ID，位置标注「历史位置未记录」
- 指标定义：原三个固定指标转为受版本控制的 METRIC-*-v1
- 检测任务：8 条 done → collected（采集完成，审核仍为 pending）
- 结果明细：16 条从固定三指标转为类型化结果；审核状态 pending、来源标记 legacy_unreviewed；原始 URI 记入 source_ref，取不到原件的不生成替代曲线
- 方法步骤：4 个配方补齐 step_id 与 kind=device；批次快照原文未改动
- 无在途批次
