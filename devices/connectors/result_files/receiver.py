"""结果文件接收器：检测软件只能自动导出文件（CSV、键值表）时，把导出目录里的文件关联到检测任务与样本，
按列映射成指标，经 ILCS 的结果回传接口入账。它只解决「取数」，不负责启动检测。

每个文件的流程：
1. 等文件写完：大小与修改时间连续 `settle_sec` 秒不变才处理（软件还在写的文件不碰）；
2. 按 `profiles` 的文件名正则匹配，命名组 `task_id`（必填，检测任务编号）与 `sample_id`（可选）；
3. 按格式解析：`csv`（表头 + 取第一行 / 最后一行）、`key_value`（每行「键,值」）或 `neware`（Neware 充放电柜的
   .nda / .ndax，经 NewareNDA 读出逐点记录，按 `cycling.py` 算每圈容量与整个测试的汇总）；列名按 `metrics`
   映射到指标版本；空值、`NA` 这类写成「无法测得」并带原因，不当 0。曲线型指标（充放电曲线、谱图）取整列：
   `"series": {"x": 列, "y": 列, "trace": 分组列}`，按分组列（如圈数）分成几条；不是数的行（单位行、表尾说明）跳过；
   `neware` 的曲线缺省取每圈一行的表（`"table": "cycles"`，如放电容量随圈数），`"table": "records"` 取逐点记录
   （可用 `"cycles": [1, 50]`、`"status": ["CC_DChg"]` 只取一部分），点太多时按 `max_points` 均匀抽稀；
4. 上传原始文件（`POST /api/integrations/files`）拿到 `raw_file_id`；
5. 回传结果（`POST /api/integrations/results`）：`event_id` = 规则名 + 文件内容 SHA-256。本地台账（`state_file`）
   按内容摘要记下已上传的原始文件编号与采集时间：同一内容再导出一次，回传内容逐字相同，ILCS 只回放原结果，
   也不会重复上传原始文件；
6. 入账成功移到 `archive/<日期>/`；被 ILCS 明确拒绝（4xx）移到 `rejected/` 并写同名 `.reason.txt`；
   网络故障 / 5xx 留在原地，下一轮重试。

服务身份（`X-Service-Source` / `X-Service-Secret`）要在「系统治理」里创建，并授权它回传的检测任务
（`analysis_tasks`）。口令放文件（`secret_file`），不写进配置。

    python devices/connectors/result_files/receiver.py --config /etc/ilcs/result-files.json
    python devices/connectors/result_files/receiver.py --config result-files.json --once   # 处理一轮就退出
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import threading
import time
import urllib.error
import urllib.request
import uuid

try:
    from . import cycling
except ImportError:  # 直接当脚本跑（python devices/connectors/result_files/receiver.py）
    import cycling  # type: ignore[no-redef]

log = logging.getLogger("ilcs.result-files")
NOT_MEASURED = {"", "na", "n/a", "nan", "-", "--", "null", "none"}
FORMATS = {"csv", "key_value", "neware"}
CURVE_POINTS = 20000  # ILCS 每条曲线的缺省上限（api/app/domain/series.py MAX_POINTS）


def _float(value) -> float:
    """曲线里的一个数。空值、不是数的格子抛 ValueError（这一行跳过）；数值 0 照收。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("空值")
    return float(value)


class Rejected(Exception):
    """ILCS 明确拒绝这个文件（4xx）或文件本身解析不了：移到 rejected/，不重试。"""


class Retry(Exception):
    """网络故障或服务端 5xx：留在原地，下一轮再试。"""


class UrllibHttp:
    """对 ILCS 的 HTTP 调用。测试里可以换成走 TestClient 的实现。"""

    def __init__(self, base_url: str, headers: dict, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.timeout = timeout

    def _send(self, request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read() or b"{}")
            except ValueError:
                body = {}
            return exc.code, body
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise Retry(f"ILCS 不可达：{exc}") from exc

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method="POST",
                                         headers={**self.headers, "Content-Type": "application/json"})
        return self._send(request)

    def post_file(self, path: str, filename: str, content: bytes, media_type: str, fields: dict) -> tuple[int, dict]:
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        for name, value in fields.items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        body.write((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                    f"filename=\"{filename}\"\r\nContent-Type: {media_type}\r\n\r\n").encode())
        body.write(content)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body.getvalue(), method="POST",
            headers={**self.headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return self._send(request)


class Profile:
    def __init__(self, raw: dict):
        self.name = str(raw.get("name") or "")
        if not re.fullmatch(r"[\w.-]{1,64}", self.name):
            raise ValueError(f"规则名 {self.name!r} 只能是字母、数字、下划线、点、横线")
        try:
            self.pattern = re.compile(str(raw.get("pattern") or ""))
        except re.error as exc:
            raise ValueError(f"规则 {self.name} 的文件名正则无效：{exc}") from exc
        if "task_id" not in self.pattern.groupindex:
            raise ValueError(f"规则 {self.name} 的文件名正则必须带命名组 (?P<task_id>...)")
        self.format = str(raw.get("format") or "csv")
        if self.format not in FORMATS:
            raise ValueError(f"规则 {self.name} 的 format 只能是 csv、key_value 或 neware")
        mass = raw.get("active_mass_mg")
        # 活性物质质量（mg）：算比容量用；不写就用 .nda 文件里登记的（有的话）
        self.active_mass_mg = float(mass) if isinstance(mass, (int, float)) and mass > 0 else None
        # 保持率的基准圈（缺省第一个充放电都有的圈）、末圈放电不到前一圈多少算没跑完（见 cycling.summary）
        self.reference_cycle = raw.get("reference_cycle")
        self.incomplete_ratio = float(raw.get("incomplete_ratio", 0.5))
        self.delimiter = str(raw.get("delimiter") or ",")
        self.row = str(raw.get("row") or "last")
        if self.row not in {"first", "last"}:
            raise ValueError(f"规则 {self.name} 的 row 只能是 first 或 last")
        self.encoding = str(raw.get("encoding") or "utf-8-sig")
        self.metrics = raw.get("metrics") or {}
        if not isinstance(self.metrics, dict) or not self.metrics:
            raise ValueError(f"规则 {self.name} 必须把至少一列映射到指标（metrics）")
        for column, target in self.metrics.items():
            if not isinstance(target, dict) or not target.get("metric_version_id"):
                raise ValueError(f"规则 {self.name} 的列 {column} 必须给出 metric_version_id")
            curve = target.get("series")
            if curve is not None:
                if self.format not in {"csv", "neware"}:
                    raise ValueError(f"规则 {self.name} 的 {column} 是曲线：只有 csv 与 neware 格式能取整列")
                if not isinstance(curve, dict) or not curve.get("x") or not curve.get("y"):
                    raise ValueError(f"规则 {self.name} 的曲线 {column} 要写 {{\"x\": 列名, \"y\": 列名}}")
                if curve.get("table", "cycles") not in {"cycles", "records"} or (
                        self.format != "neware" and "table" in curve):
                    raise ValueError(f"规则 {self.name} 的曲线 {column}：table 只有 neware 格式能写，取 cycles 或 records")
        self.instrument_serial = str(raw.get("instrument_serial") or "")
        self.station_id = str(raw.get("station_id") or "")
        self.parser_version = str(raw.get("parser_version") or f"{self.name}-1")
        self.upload_raw = bool(raw.get("upload_raw", True))
        self.media_type = str(raw.get("media_type") or "")

    def table(self, text: str) -> list[dict]:
        """CSV 的全部数据行（曲线取整列用）。"""
        return [{str(key).strip(): (value or "").strip() for key, value in row.items() if key is not None}
                for row in csv.DictReader(io.StringIO(text), delimiter=self.delimiter)]

    def neware(self, path: Path) -> tuple[dict, list[dict], list[dict]]:
        """Neware .nda / .ndax → (汇总值, 每圈一行, 逐点记录)。汇总值与每圈的表写成文字，与 CSV 走同一套映射。"""
        try:
            records, mass = cycling.read_neware(str(path))
        except RuntimeError as exc:
            raise Retry(str(exc)) from exc  # 没装 NewareNDA：配置问题，文件留着等装好
        except Exception as exc:  # noqa: BLE001  NewareNDA 读不了的文件：文件本身不合格
            raise Rejected(f"读不了这个 Neware 数据文件：{exc}") from exc
        if not records:
            raise Rejected("Neware 数据文件里没有数据点")
        rows = cycling.cycles(records)
        summary = cycling.summary(rows, active_mass_mg=self.active_mass_mg or mass, reference_cycle=self.reference_cycle,
                                  incomplete_ratio=self.incomplete_ratio)
        text = {key: "" if value is None else f"{value:.10g}" for key, value in summary.items()}
        table = [{key: "" if value is None else str(value) for key, value in row.items()} for row in rows]
        return text, table, records

    def rows(self, text: str) -> dict:
        if self.format == "key_value":
            values = {}
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                key, _, value = line.partition(self.delimiter)
                values[key.strip()] = value.strip()
            return values
        reader = list(csv.DictReader(io.StringIO(text), delimiter=self.delimiter))
        if not reader:
            raise Rejected("CSV 只有表头或是空文件，没有数据行")
        row = reader[0] if self.row == "first" else reader[-1]
        return {str(key).strip(): (value or "").strip() for key, value in row.items() if key is not None}

    def curve(self, label: str, target: dict, table: list[dict]) -> dict:
        """曲线型指标取整列：x、y 两列各乘系数；给了 trace 列就按它分成几条（如每圈一条）。
        不是数的行跳过；一个点都没有写成「无法测得」。"""
        spec = target["series"]
        columns = [spec["x"], spec["y"], *([spec["trace"]] if spec.get("trace") else [])]
        header = set(table[0]) if table else set()
        for column in columns:
            if column not in header:
                raise Rejected(f"文件里没有列 {column!r}（规则 {self.name}，曲线 {label}）")
        x_factor, y_factor = float(spec.get("x_factor", 1)), float(target.get("factor", 1))
        traces: dict[str, tuple[list[float], list[float]]] = {}
        skipped = 0
        for row in table:
            try:
                x, y = _float(row.get(spec["x"])) * x_factor, _float(row.get(spec["y"])) * y_factor
            except (TypeError, ValueError):
                skipped += 1
                continue
            xs, ys = traces.setdefault(str(row.get(spec["trace"], "")) if spec.get("trace") else "", ([], []))
            xs.append(x)
            ys.append(y)
        limit = int(spec.get("max_points") or CURVE_POINTS)
        for name, (xs, ys) in list(traces.items()):
            if len(xs) > limit:
                # 均匀抽稀到上限（保留首尾）：ILCS 拒收点数超限的曲线
                picks = sorted({round(i * (len(xs) - 1) / (limit - 1)) for i in range(limit)})
                traces[name] = ([xs[i] for i in picks], [ys[i] for i in picks])
        entry = {"metric_version_id": target["metric_version_id"], "unit": str(target.get("unit") or "")}
        if not traces:
            entry["not_measured_reason"] = f"导出文件的 {spec['x']} / {spec['y']} 两列没有成对的数值"
        elif spec.get("trace"):
            entry["value"] = {"traces": [{"name": name, "x": xs, "y": ys} for name, (xs, ys) in traces.items()]}
        else:
            xs, ys = traces[""]
            entry["value"] = {"x": xs, "y": ys}
        if skipped:
            log.info("曲线 %s 跳过了 %d 行不是数的行（单位行、说明行）", label, skipped)
        return entry

    def metrics_from(self, values: dict, table: list[dict] | None = None,
                     records: list[dict] | None = None) -> list[dict]:
        metrics = []
        for column, target in self.metrics.items():
            spec = target.get("series")
            if spec is not None:
                rows = table or []
                if spec.get("table") == "records":
                    rows = cycling.select(records or [], cycles_wanted=spec.get("cycles"), statuses=spec.get("status"))
                metrics.append(self.curve(column, target, rows))
                continue
            if column not in values:
                raise Rejected(f"文件里没有列 {column!r}（规则 {self.name}）")
            raw = values[column]
            entry = {"metric_version_id": target["metric_version_id"], "unit": str(target.get("unit") or "")}
            if raw.lower() in NOT_MEASURED:
                entry["not_measured_reason"] = f"导出文件的 {column} 没有给出数值（{raw or '空'}）"
            else:
                try:
                    entry["value"] = float(raw) * float(target.get("factor", 1))
                except ValueError:
                    entry["value"] = raw  # 文本型指标（如外观判定）原样回传，由 ILCS 按指标类型校验
            metrics.append(entry)
        return metrics


class Receiver:
    def __init__(self, config: dict, http=None):
        self.inbox = Path(config["inbox"])
        self.archive = Path(config.get("archive") or self.inbox.parent / "archive")
        self.rejected = Path(config.get("rejected") or self.inbox.parent / "rejected")
        self.settle = float(config.get("settle_sec", 3))
        self.poll = float(config.get("poll_sec", 5))
        self.profiles = [Profile(item) for item in config.get("profiles") or []]
        if not self.profiles:
            raise ValueError("至少要配置一条 profiles 规则")
        for directory in (self.inbox, self.archive, self.rejected):
            directory.mkdir(parents=True, exist_ok=True)
        if http is None:
            secret = Path(config["secret_file"]).read_text(encoding="utf-8").strip()
            http = UrllibHttp(config["ilcs_url"], {"X-Service-Source": config["source"], "X-Service-Secret": secret})
        self.http = http
        self.seen: dict[Path, tuple[int, float, float]] = {}
        self.state_path = Path(config.get("state_file") or self.inbox.parent / "receiver-state.json")
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        except ValueError as exc:
            raise ValueError(f"接收器台账 {self.state_path} 坏了：{exc}；核对后删除或修复再启动") from exc
        self.state.setdefault("files", {})

    def _save_state(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, self.state_path)

    # ---------- 一轮 ----------

    def _settled(self, path: Path) -> bool:
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime)
        first = self.seen.get(path)
        now = time.monotonic()
        if first is None or first[:2] != signature:
            self.seen[path] = (*signature, now)
            return self.settle <= 0
        return now - first[2] >= self.settle

    def run_once(self) -> dict:
        counts = {"archived": 0, "rejected": 0, "waiting": 0, "retry": 0, "ignored": 0}
        for path in sorted(self.inbox.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.suffix == ".part":
                continue
            profile = next((item for item in self.profiles if item.pattern.fullmatch(path.name)), None)
            if profile is None:
                counts["ignored"] += 1
                continue
            if not self._settled(path):
                counts["waiting"] += 1
                continue
            try:
                self._process(path, profile)
            except Retry as exc:
                log.warning("%s 暂时处理不了，下一轮重试：%s", path.name, exc)
                counts["retry"] += 1
                continue
            except Rejected as exc:
                self._move(path, self.rejected, reason=str(exc))
                counts["rejected"] += 1
                continue
            self._move(path, self.archive / datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            counts["archived"] += 1
        return counts

    def _process(self, path: Path, profile: Profile) -> None:
        match = profile.pattern.fullmatch(path.name)
        content = path.read_bytes()
        if profile.format == "neware":
            metrics = profile.metrics_from(*profile.neware(path))
        else:
            try:
                text = content.decode(profile.encoding)
            except UnicodeDecodeError as exc:
                raise Rejected(f"文件不是 {profile.encoding} 编码：{exc}") from exc
            table = profile.table(text) if profile.format == "csv" else []
            metrics = profile.metrics_from(profile.rows(text), table)
        digest = hashlib.sha256(content).hexdigest()
        key = f"{profile.name}:{digest}"
        # 同一内容第二次出现：沿用第一次的采集时间与原始文件编号，回传内容逐字相同
        entry = self.state["files"].setdefault(key, {
            "collected_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            "first_name": path.name,
        })
        payload = {
            "event_id": f"file:{profile.name}:{digest[:40]}", "task_id": match.group("task_id"),
            "sample_id": match.groupdict().get("sample_id") or "", "collected_at": entry["collected_at"],
            "parser_version": profile.parser_version, "instrument_serial": profile.instrument_serial,
            "station_id": profile.station_id, "metrics": metrics,
        }
        if profile.upload_raw:
            if not entry.get("raw_file_id"):
                media_type = profile.media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                status, body = self.http.post_file("/api/integrations/files", path.name, content, media_type,
                                                   {"note": f"{profile.name} 导出文件 sha256 {digest[:16]}"})
                self._check(status, body, "上传原始文件")
                entry["raw_file_id"] = body["id"]
                self._save_state()  # 先记下再回传：回传失败重试时不会再传一份原始文件
            payload["raw_file_id"] = entry["raw_file_id"]
        status, body = self.http.post_json("/api/integrations/results", payload)
        self._check(status, body, "回传结果")
        entry["ingested"] = True
        entry["task_id"] = payload["task_id"]
        self._save_state()
        log.info("%s → 任务 %s：%s%s", path.name, payload["task_id"], body.get("task_state", ""),
                 "（重传回放）" if body.get("replayed") else "")

    @staticmethod
    def _check(status: int, body: dict, action: str) -> None:
        if 200 <= status < 300:
            return
        detail = body.get("detail") if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or json.dumps(detail, ensure_ascii=False)
        if status >= 500 or status in {408, 429}:
            raise Retry(f"{action} HTTP {status}：{detail}")
        raise Rejected(f"{action}被 ILCS 拒绝（HTTP {status}）：{detail}")

    def _move(self, path: Path, directory: Path, reason: str = "") -> None:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / path.name
        if target.exists():
            target = directory / f"{path.stem}.{int(time.time())}{path.suffix}"
        shutil.move(str(path), target)
        self.seen.pop(path, None)
        if reason:
            target.with_name(target.name + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
            log.warning("%s 已移到 %s：%s", path.name, directory, reason)

    def serve(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                counts = self.run_once()
                if counts["archived"] or counts["rejected"]:
                    log.info("本轮：%s", counts)
            except Exception:  # 一轮出错不能让接收器退出
                log.exception("处理导出目录失败")
            stop.wait(self.poll)


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.environ.get("RESULT_FILES_CONFIG", "result-files.json"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    receiver = Receiver(json.loads(Path(args.config).read_text(encoding="utf-8")))
    if args.once:
        print(json.dumps(receiver.run_once(), ensure_ascii=False))
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    receiver.serve(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
