# 结果文件接收器

检测软件只能自动导出文件（CSV、键值表），又不接受外部启动时用它：盯住导出目录，把每个文件关联到
检测任务与样本、按列映射成指标，经 ILCS 的结果回传接口入账。它只解决**取数**，不负责启动检测。

## 每个文件怎么处理

1. **等写完**：大小与修改时间连续 `settle_sec` 秒不变才处理；`.part` 结尾与点号开头的文件跳过。
2. **匹配规则**：按 `profiles` 的文件名正则匹配，命名组 `task_id`（检测任务编号，必填）、`sample_id`（可选，
   和任务绑定的样本不一致会被 ILCS 整次拒绝）。哪条规则都不匹配的文件原样留着。
3. **解析**：`csv`（表头 + 取 `first` / `last` 一行）或 `key_value`（每行「键,值」）。列名按 `metrics` 映射到指标版本
   （`metric_version_id`，在「检测指标」页查）；空值、`NA`、`-` 这类写成「无法测得」并带原因，**不当 0**。
   曲线型指标（充放电曲线、谱图）取整列，见下面的「曲线」。
4. **原始文件**：先上传（`POST /api/integrations/files`，类型与大小限制同人工上传）拿到编号，结果里引用它。
5. **回传**：`POST /api/integrations/results`，事件号 = `file:<规则名>:<内容 SHA-256>`。本地台账（`state_file`，缺省
   `inbox` 的上一级 `receiver-state.json`）按内容记下采集时间与原始文件编号——同一内容再导出一次，回传逐字相同，
   ILCS 只回放原结果，也不会重复上传原始文件。
6. **归档**：入账成功移到 `archive/<日期>/`；被 ILCS 明确拒绝（4xx：任务不存在、样本不符、指标不在任务要求里……）
   或文件本身不合格（缺列、编码不对）移到 `rejected/`，同名 `.reason.txt` 写原因；网络故障或 5xx 留在原地下一轮重试。

入账的结果和其他回传一样是「待复核」：不越过数据审核，也不直接进正式统计。

## 配置

```json
{
  "ilcs_url": "http://api:8000",
  "source": "result-files",
  "secret_file": "/run/secrets/ilcs/result-files/secret",
  "inbox": "/data/instrument-exports/inbox",
  "archive": "/data/instrument-exports/archive",
  "rejected": "/data/instrument-exports/rejected",
  "settle_sec": 3,
  "poll_sec": 5,
  "profiles": [
    {
      "name": "ec-export",
      "pattern": "^(?P<task_id>[^_]+)__(?P<sample_id>[^_]+)__.*\\.csv$",
      "format": "csv", "row": "last", "encoding": "utf-8-sig",
      "metrics": {
        "容量(mAh/g)": {"metric_version_id": "METRIC-discharge_capacity-v1", "unit": "mAh/g"},
        "面密度": {"metric_version_id": "METRIC-areal_density-v1", "unit": "mg/cm2", "factor": 1}
      },
      "instrument_serial": "EC-TESTER-0001",
      "station_id": "",
      "parser_version": "ec-csv-1",
      "upload_raw": true,
      "media_type": "text/csv"
    }
  ]
}
```

- `source` / `secret_file`：服务身份。在「系统治理 · 服务身份」创建，授权它要回传的检测任务（`analysis_tasks`，
  可以是全部），声明了 `instrument_serial` 的还要把序列号列进授权。口令只放文件，不写进配置。
- `factor`：列值乘这个系数后入账（单位换算）。文本型指标（如外观判定）原样回传，由 ILCS 按指标类型校验。
- **曲线**：映射里写 `series` 就取整列，映射的键只是个名字：

  ```json
  "放电曲线": {"metric_version_id": "METRIC-dis_curve-v1", "unit": "V",
               "series": {"x": "容量(mAh/g)", "y": "电压(V)", "trace": "圈数", "x_factor": 1}, "factor": 1}
  ```

  `x` / `y` 两列成对取数（`x_factor`、`factor` 分别乘到 x、y 上），给了 `trace` 就按这一列的值分成几条（每圈一条）；
  不是数的行（单位行、表尾说明）跳过，一对数都没有写成「无法测得」。`csv` 与 `neware` 格式能取整列。同一个文件里的数值指标
  照旧取 `row` 那一行（如最后一行的容量）；或者在曲线指标上声明派生（`derived`），由 ILCS 从曲线算。
  点数超过 `max_points`（缺省 20000，ILCS 每条曲线的上限）按均匀抽稀，保留首尾。
- 检测软件的文件名里没有任务编号时，先在软件里把导出文件名模板配成带任务编号（扫码录入任务号最常见）。

## Neware 充放电数据（`format: "neware"`）

Neware BTS 存的 `.nda` / `.ndax` 是二进制文件，用 [NewareNDA](https://github.com/d-cogswell/NewareNDA)（BSD-3）读出逐点记录，
再按 `cycling.py` 算每圈的充 / 放电容量、能量、库伦效率和整个测试的汇总。跑接收器的机器要 `pip install NewareNDA==2026.6.11`
（会装 pandas）；没装时文件留在收件箱、日志报错，装好下一轮就处理。

```json
{
  "name": "neware", "format": "neware", "pattern": "^(?P<task_id>[^_]+)__(?P<sample_id>[^_]+)__.*\\.ndax?$",
  "active_mass_mg": 16.0, "reference_cycle": 3, "parser_version": "neware-nda-1", "media_type": "application/zip",
  "metrics": {
    "first_discharge_mAh_g": {"metric_version_id": "METRIC-discharge_capacity-v1", "unit": "mAh/g"},
    "retention_pct":         {"metric_version_id": "METRIC-retention-v1", "unit": "%"},
    "放电容量-圈数":          {"metric_version_id": "<曲线指标>", "unit": "mAh", "series": {"x": "cycle", "y": "discharge_mAh"}},
    "放电曲线":              {"metric_version_id": "<曲线指标>", "unit": "V", "series": {
                               "table": "records", "x": "Discharge_Capacity(mAh)", "y": "Voltage", "trace": "Cycle",
                               "cycles": [1, 50], "status": ["CC_DChg"]}}
  }
}
```

- 数值指标的键是汇总里的项：`cycle_count`、`first_charge_mAh`、`first_discharge_mAh`、`first_ce_pct`（首效）、
  `reference_cycle` / `reference_discharge_mAh`（保持率的基准圈，缺省第一个充、放电都有的圈，`reference_cycle` 可以指定）、
  `last_cycle` / `last_discharge_mAh`（文件里最后一圈，原样）、`last_cycle_partial`（末圈放电不到前一圈的 `incomplete_ratio`，
  缺省一半，当作没跑完时为 1）、`final_cycle` / `final_discharge_mAh` / `final_ce_pct`（去掉没跑完的末圈后的最后一圈）、
  `max_discharge_mAh`、`mean_ce_pct`（基准圈之后到终圈的平均库伦效率）、`retention_pct`（终圈 ÷ 基准圈 × 100）。
  给了 `active_mass_mg`（或 `.nda` 文件里登记了活性物质质量）再多一组比容量 `*_mAh_g`。取不到的项写成「无法测得」，不当 0。
- 曲线缺省取每圈一行的表（列 `cycle`、`charge_mAh`、`discharge_mAh`、`charge_mWh`、`discharge_mWh`、`ce_pct`）；
  `"table": "records"` 取逐点记录（NewareNDA 的列名：`Voltage`、`Current(mA)`、`Charge_Capacity(mAh)`、
  `Discharge_Capacity(mAh)`、`Time`……），可以只取几圈（`cycles`）、几种工步（`status`，如 `CC_DChg`）。
- 容量每个工步从 0 重新累计（NewareNDA 的口径），一圈的容量是这圈各工步最大值之和；已对 NewareNDA 仓库里的真实
  `.ndax` / `.nda` 样例与 pandas 的同一算法逐圈核对过（`api/tests/api/test_result_files.py`，设 `NEWARE_SAMPLE` 可复跑）。
- **原始文件上传**：`.ndax` 是 zip 包，`media_type` 写 `application/zip`；ILCS 缺省不收这类二进制，要在 `.env` 的
  `ILCS_FILE_ALLOWED_TYPES` 里加上它（`.nda` 是 `application/octet-stream`，放开前想清楚）。不想上传就写 `"upload_raw": false`。
- 文件名要带检测任务编号：BTS 的备份文件名可以按条码命名，手工跑的测试把条码填成检测任务编号；经 ILCS 网关
  （`devices/gateway/neware-bts`）跑的测试，条码是 `ILCS-<摘要>`，那一路的曲线由网关取数（待做），不走这里。

## 运行

```bash
python devices/connectors/result_files/receiver.py --config result-files.json          # 常驻，按 poll_sec 轮询
python devices/connectors/result_files/receiver.py --config result-files.json --once   # 处理一轮就退出（排障用）
```

Compose 里是 `result-files` 服务（`--profile results`），配置放 `secrets/result-files/result-files.json`，
导出目录挂在 `data/instrument-exports/`（检测工作站把共享目录映射到这里）。
