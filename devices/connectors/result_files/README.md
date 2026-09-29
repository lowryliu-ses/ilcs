# 结果文件接收器

检测软件只能自动导出文件（CSV、键值表），又不接受外部启动时用它：盯住导出目录，把每个文件关联到
检测任务与样本、按列映射成指标，经 ILCS 的结果回传接口入账。它只解决**取数**，不负责启动检测。

## 每个文件怎么处理

1. **等写完**：大小与修改时间连续 `settle_sec` 秒不变才处理；`.part` 结尾与点号开头的文件跳过。
2. **匹配规则**：按 `profiles` 的文件名正则匹配，命名组 `task_id`（检测任务编号，必填）、`sample_id`（可选，
   和任务绑定的样本不一致会被 ILCS 整次拒绝）。哪条规则都不匹配的文件原样留着。
3. **解析**：`csv`（表头 + 取 `first` / `last` 一行）或 `key_value`（每行「键,值」）。列名按 `metrics` 映射到指标版本
   （`metric_version_id`，在「检测指标」页查）；空值、`NA`、`-` 这类写成「无法测得」并带原因，**不当 0**。
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
- 检测软件的文件名里没有任务编号时，先在软件里把导出文件名模板配成带任务编号（扫码录入任务号最常见）。

## 运行

```bash
python devices/connectors/result_files/receiver.py --config result-files.json          # 常驻，按 poll_sec 轮询
python devices/connectors/result_files/receiver.py --config result-files.json --once   # 处理一轮就退出（排障用）
```

Compose 里是 `result-files` 服务（`--profile results`），配置放 `secrets/result-files/result-files.json`，
导出目录挂在 `data/instrument-exports/`（检测工作站把共享目录映射到这里）。
