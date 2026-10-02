"""PalmSens MethodSCRIPT 后端（EmStat Pico / EmStat4 / Nexus / Sensit）：把一次测量写成 MethodSCRIPT 脚本，经串口
（USB 虚拟串口）或 TCP 发给仪器、读回数据包。实现 driver/backend.py 的调用面。

通讯协议（EmStat4 通讯协议 v1.3、EmStat Pico 通讯协议 v1.6）：每条命令一行、`\\n` 结尾，仪器先回显命令的第一个字符，
做完再回 `\\n`；出错在换行前回 `!XXXX`（加载脚本出错带行号、列号）。这里用到的：

    t       固件版本（两行：`tes4_hr1100#Jan 28 2022 11:04:43` / `R*`）       i   序列号     v   MethodSCRIPT 版本
    l       加载脚本：`l`、脚本各行、一个空行；仪器边收边做语法检查，成功回 `l`，出错回 `l!XXXX: Line L, Col C`
    r       运行加载好的脚本：先回 `r`，然后是脚本输出，最后一个空行
    e       加载并运行（只用来在出错后补发 `cell_off`）
    Z       终止正在跑的脚本：输出流里回 `Z`，测量循环照样收尾（`*`）、`on_finished:` 之后的照样执行，最后空行；
            没有脚本在跑时回 `Z!0006`

为什么用 `l` + `r` 而不是 `e`：加载只是把脚本放进内存（不碰电池），仪器拒收（语法、参数、不支持）就是明确没动；
`r` 写出去之后才可能动。这样「明确拒绝」和「结果未知」的分界清清楚楚。

脚本输出：`MXXXX` 一个测量循环开始（XXXX 是技术号）、`*` 结束；`Cnnnn` / `-` CV 一圈开始 / 结束（`nscans`）；
`P...` 数据包；`T...` 文字；运行时出错是 `!XXXX: Line L`。每个脚本都以 `on_finished:` + `cell_off` 结尾：
正常结束、被终止都会断开电池。**运行时出错不执行 on_finished**，所以出错后网关另发一个只有 `cell_off` 的脚本。

链路断过（USB 拔插、网关重启）之后先「同步」：发 `Z`，仪器上还在跑的脚本停下（电池断开），读到结束再接着用——
断线期间的数据已经收不回来了，那次测量判失败，不让仪器在没人收数据的情况下接着加电位。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

from . import methodscript as ms
from .backend import BackendError, BackendRejected, Finish, Limits, Plan, Point, StartUnknown
from .link import Link, LinkError

log = logging.getLogger("ilcs.gateway.echem")

# 数据包里的变量类型 → 统一的键
KEYS = {"da": "e_V", "ab": "e_V", "ba": "i_A", "eb": "t_s", "dc": "f_Hz", "cc": "z_re_ohm", "cd": "z_im_ohm"}
SIMULATOR_SERIAL = "ILCS-SIMULATOR"
ERROR_PAUSE = 0.15      # 出错后仪器 50–100 ms 不收命令
SYNC_TIMEOUT = 30.0     # 同步时等仪器上的旧脚本收尾最多多久
SILENCE_MARGIN = 30.0   # 两行输出之间最多隔多久（在测量本身的间隔之上再宽限）
DC_MODE, EIS_MODE = 2, 3  # PGStat 模式：低速（直流技术）、高速（EIS 必须用）


# ---------- 脚本 ----------

def _lit(value: float) -> str:
    return ms.literal(float(value))


def _ranging(plan: Plan, potential_V: float) -> list[str]:
    r = plan.ranging
    lines = [f"set_range ba {_lit(r.start_A)}", f"set_autoranging ba {_lit(r.min_A)} {_lit(r.max_A)}"]
    # 测电位（开路）的量程：固定在能放下电池电压的那一档（EmStat Pico 不支持，会忽略）
    lines += [f"set_range ab {_lit(potential_V)}", f"set_autoranging ab {_lit(potential_V)} {_lit(potential_V)}"]
    return lines


def _rest(settings: dict[str, Any]) -> list[str]:
    """开路静置、测开路电位：之后的扫描 / 阻抗从 oc（静置最后一点的开路电位）开始。"""
    rest_s = float(settings["rest_s"])
    interval = float(settings["rest_interval_s"])
    return ["cell_off", "timer_start", f"meas_loop_ocp oc {_lit(interval)} {_lit(rest_s)}",
            "timer_get t", "pck_start", "pck_add t", "pck_add oc", "pck_end", "endloop"]


def build_script(plan: Plan, *, potential_range_V: float) -> list[str]:
    """一次测量 → MethodSCRIPT 脚本各行（不含 `l` 和结尾的空行）。数都写成「整数 + SI 前缀」。"""
    s, technique = plan.settings, plan.technique
    from_ocp = s.get("e_begin_V") == "ocp" or s.get("e_dc_V") == "ocp"
    names = {"ocp": ["t", "p"], "lsv": ["p", "c"], "cv": ["p", "c"], "ca": ["t", "p", "c"],
             "eis": ["f", "zr", "zi"]}[technique]
    if from_ocp:
        names = ["t", "oc", *[name for name in names if name != "t"]]
    lines = [f"var {name}" for name in names]
    lines += ["set_pgstat_chan 0", f"set_pgstat_mode {EIS_MODE if technique == 'eis' else DC_MODE}"]
    if technique != "eis":
        lines.append(f"set_max_bandwidth {_lit(plan.bandwidth_Hz)}")  # EIS 的带宽由仪器按频率定，这条对 EIS 无效
    lines += _ranging(plan, potential_range_V)
    if technique == "ocp":
        lines += ["cell_off", "timer_start", f"meas_loop_ocp p {_lit(s['interval_s'])} {_lit(s['duration_s'])}",
                  "timer_get t", "pck_start", "pck_add t", "pck_add p", "pck_end", "endloop"]
    elif technique == "lsv":
        end = _lit(s["e_end_V"])
        if from_ocp:
            lines += _rest(s)
            begin = "oc"
            # EmStat Pico 要先把动态电位窗口挪到扫描范围上（EmStat4 / Nexus 忽略这条）
            lines += [f"if oc < {end}", f"set_range_minmax da oc {end}", "else", f"set_range_minmax da {end} oc",
                      "endif"]
        else:
            begin = _lit(s["e_begin_V"])
            low, high = sorted((float(s["e_begin_V"]), float(s["e_end_V"])))
            lines.append(f"set_range_minmax da {_lit(low)} {_lit(high)}")
        lines += [f"set_e {begin}", "cell_on",
                  f"meas_loop_lsv p c {begin} {end} {_lit(s['e_step_V'])} {_lit(s['scan_rate_V_s'])}",
                  "pck_start", "pck_add p", "pck_add c", "pck_end"]
        stop = s.get("stop_A")
        if stop:
            # 电流到截止值就提前结束扫描（两个方向都看：从开路电位起扫时不知道方向）
            lines += [f"if c > {_lit(stop)}", "breakloop", "endif", f"if c < {_lit(-stop)}", "breakloop", "endif"]
        lines.append("endloop")
    elif technique == "cv":
        values = [float(s["e_begin_V"]), float(s["e_vertex1_V"]), float(s["e_vertex2_V"])]
        cycles = int(s["cycles"])
        lines += [f"set_range_minmax da {_lit(min(values))} {_lit(max(values))}", f"set_e {_lit(values[0])}",
                  "cell_on",
                  f"meas_loop_cv p c {' '.join(_lit(value) for value in values)} {_lit(s['e_step_V'])} "
                  f"{_lit(s['scan_rate_V_s'])}" + (f" nscans({cycles})" if cycles > 1 else ""),
                  "pck_start", "pck_add p", "pck_add c", "pck_end", "endloop"]
    elif technique == "ca":
        e = _lit(s["e_V"])
        lines += [f"set_range_minmax da {e} {e}", f"set_e {e}", "cell_on", "timer_start",
                  f"meas_loop_ca p c {e} {_lit(s['interval_s'])} {_lit(s['duration_s'])}",
                  "timer_get t", "pck_start", "pck_add t", "pck_add p", "pck_add c", "pck_end", "endloop"]
    elif technique == "eis":
        if from_ocp:
            lines += _rest(s)
            dc = "oc"
        else:
            dc = _lit(s["e_dc_V"])
        lines += [f"set_e {dc}", "cell_on",
                  f"meas_loop_eis f zr zi {_lit(s['amplitude_Vrms'])} {_lit(s['freq_start_Hz'])} "
                  f"{_lit(s['freq_end_Hz'])} {ms.integer_literal(s['points'])} {dc}",
                  "pck_start", "pck_add f", "pck_add zr", "pck_add zi", "pck_end", "endloop"]
    else:
        raise ValueError(f"MethodSCRIPT 后端不会做 {technique}")
    lines += ["on_finished:", "cell_off"]
    too_long = [line for line in lines if len(line) >= 255]
    if too_long:
        raise ValueError(f"脚本行超过 255 个字符：{too_long[0][:60]}…")
    return lines


def silence_limit(plan: Plan) -> float:
    """两行输出之间最多隔多久：测量本身最长的间隔 × 3 + 宽限。超过就当仪器失联。"""
    s = plan.settings
    gaps = [float(s.get("rest_interval_s") or 0)]
    if plan.technique in {"ocp", "ca"}:
        gaps.append(float(s["interval_s"]))
    elif plan.technique in {"lsv", "cv"}:
        gaps.append(float(s["e_step_V"]) / float(s["scan_rate_V_s"]))
    elif plan.technique == "eis":
        gaps.append(10.0 / min(float(s["freq_start_Hz"]), float(s["freq_end_Hz"])))  # 最低频点要好几个周期
    return max(gaps) * 3 + SILENCE_MARGIN


# ---------- 后端 ----------

class MethodScript:
    """一台 MethodSCRIPT 仪器（一个通道）。同一时刻只有一次测量占着链路。"""

    def __init__(self, link: dict[str, Any] | Link, *, timeout: float = 3.0, model: str = ""):
        self.link = link if isinstance(link, Link) else Link(link, timeout=timeout)
        self.model = model
        self.running = False     # 一次测量占着链路（从 r 到读完）：这期间身份回上次读到的
        self.lost = False        # 测量途中链路断了、还没重连上
        self.synced_once = False  # 这个进程里和仪器同步过：网关重启前开始的测量（如果还在跑）已经停了
        self._synced = -1        # 和仪器同步过的那次连接（link.connects）
        self._identity: dict[str, Any] | None = None
        self._serial = ""
        self._script_version = ""
        self._plan: Plan | None = None
        self._script: list[str] = []
        self._silence = SILENCE_MARGIN

    @property
    def timeout(self) -> float:
        return self.link.timeout

    # ---------- 连接、同步 ----------

    def _ensure(self) -> None:
        """连上并同步：每次（重）连上之后先发 Z，把仪器带到「没有脚本在跑」的已知状态。"""
        try:
            self.link.open()
        except LinkError as exc:
            raise BackendError(str(exc)) from exc
        if self._synced != self.link.connects:
            self._sync()
            self._synced = self.link.connects
            self.synced_once = True

    @property
    def ready(self) -> bool:
        """仪器上没有网关不知道的测量在跑：这个进程里同步过，而且现在不在断线重连。"""
        return self.synced_once and not self.lost

    def _sync(self) -> None:
        try:
            # 旧脚本可能还在往回发数据：不等它安静，丢掉手头的就发 Z
            self.link.drain(quiet=0.02, longest=0.2)
            self.link.write("\nZ\n")  # 先补一个换行：冲掉仪器命令缓冲里可能残留的半条命令
            deadline = time.monotonic() + 2 * self.timeout
            while True:  # Z 的回显马上就来（前面可能夹着旧脚本的数据行）
                line = self.link.read_line(max(0.05, deadline - time.monotonic()))
                if line.startswith("Z"):
                    break
                if time.monotonic() > deadline:
                    raise BackendError("同步时仪器一直没回 Z")
            if line == "Z":  # 仪器上有脚本在跑（网关重启、链路断过之前开始的）：等它收尾
                log.warning("仪器上还有脚本在跑，已发终止（Z），等它结束")
                end = time.monotonic() + SYNC_TIMEOUT
                while self.link.read_line(max(0.05, end - time.monotonic())) != "":
                    if time.monotonic() > end:
                        raise BackendError(f"发了终止，{SYNC_TIMEOUT:g} 秒内仪器上的脚本还没结束")
            # Z!0006：本来就没有脚本在跑
            time.sleep(ERROR_PAUSE)
            self.link.drain(quiet=0.02, longest=0.5)
        except LinkError as exc:
            self.link.close()
            raise BackendError(f"和仪器同步失败：{exc}", sent=exc.sent) from exc

    # ---------- 身份 ----------

    def identity(self) -> dict[str, Any]:
        if self.lost:
            raise BackendError("测量途中和仪器的链路断了，正在重连")
        if self.running:
            if self._identity is None:
                raise BackendError("测量进行中，还没读到仪器身份")
            return dict(self._identity)
        self._ensure()
        try:
            self.link.drain(quiet=0.01)
            self.link.write("t\n")
            first, second = self.link.read_line(), self.link.read_line()
            firmware = ms.parse_firmware(first, second)
            if not self._serial:
                self.link.write("i\n")
                self._serial = self._answer("i")
                self.link.write("v\n")
                self._script_version = self._answer("v")
        except LinkError as exc:
            raise BackendError(f"读不到仪器身份：{exc}") from exc
        except ValueError as exc:
            self.link.close()
            raise BackendError(f"仪器应答不对：{exc}") from exc
        info = ms.device_info(firmware["device_type"]) or {}
        self._identity = {
            "serial": self._serial, "model": info.get("model") or firmware["device_type"],
            "device_type": firmware["device_type"],
            "firmware": f"{firmware['device_type']} {firmware['version']}（{firmware['release']}，{firmware['build']}）",
            "firmware_version": firmware["version"], "script_version": self._script_version,
            "simulator": self._serial.startswith(SIMULATOR_SERIAL),
        }
        return dict(self._identity)

    def _answer(self, command: str) -> str:
        line = self.link.read_line()
        error = ms.parse_error(line)
        if error is not None or not line.startswith(command):
            raise ValueError(f"{command} 命令回了 {line!r}")
        return line[1:].strip()

    def limits(self, technique: str) -> Limits | None:
        info = ms.device_info((self._identity or {}).get("device_type", ""))
        if info is None:
            return None
        low, high, window = info["modes"][EIS_MODE if technique == "eis" else DC_MODE]
        return Limits(e_min_V=low, e_max_V=high, window_V=window, eis_max_hz=info["eis_max_hz"],
                      eis_max_vrms=info["eis_max_vrms"], i_max_A=info["i_max_A"])

    # ---------- 测量 ----------

    def prepare(self, plan: Plan) -> None:
        """加载脚本（`l`）：仪器边收边做语法检查。拒收抛 BackendRejected，链路不通抛 BackendError——都没动。"""
        if self.running:
            raise BackendRejected("busy", "上一次测量还在占着仪器")
        limits = self.limits(plan.technique)
        potential = plan.ranging.potential_V or (max(abs(limits.e_min_V), abs(limits.e_max_V)) if limits else 4.0)
        try:
            script = build_script(plan, potential_range_V=potential)
        except ValueError as exc:
            raise BackendRejected("invalid", str(exc)) from exc
        self._ensure()
        try:
            self.link.drain(quiet=0.01)
            self.link.write("l\n" + "".join(line + "\n" for line in script) + "\n")
            deadline = time.monotonic() + self.timeout + 0.002 * len(script)
            while True:
                line = self.link.read_line(max(0.1, deadline - time.monotonic()))
                if line.startswith("Z"):
                    continue  # 晚到的终止应答（终止撞上了上一次测量结束：Z!0006）
                error = ms.parse_error(line)
                if error is not None:
                    time.sleep(ERROR_PAUSE)
                    self.link.drain()
                    raise BackendRejected(error.kind, f"仪器拒收测量脚本：{error.text(script)}")
                if line == "l":
                    break
                log.debug("加载脚本时丢掉一行：%r", line)
        except LinkError as exc:
            raise BackendError(f"加载测量脚本时链路出错：{exc}", sent=exc.sent) from exc
        self._plan, self._script, self._silence = plan, script, silence_limit(plan)

    def begin(self) -> None:
        """运行加载好的脚本（`r`）。"""
        if self._plan is None:
            raise BackendRejected("invalid", "没有加载测量脚本")
        try:
            self.link.write("r\n")
        except LinkError as exc:
            raise BackendError(f"开始命令没写出去：{exc}", sent=False) from exc
        self.running = True
        try:
            line = self.link.read_line()
            while line.startswith("Z"):  # 晚到的终止应答，不是 r 的回答
                line = self.link.read_line()
        except LinkError as exc:
            # 写出去了没回：仪器可能已经在测。下次用链路时先同步（发 Z）把它停下
            self._synced = -1
            self.running = False
            raise StartUnknown(f"开始命令（r）写出去了，没收到仪器确认：{exc}") from exc
        error = ms.parse_error(line)
        if error is not None and line.startswith("r!"):
            # 仪器明确不运行（如 r!000C 没有加载好的脚本）：没动
            self.running = False
            time.sleep(ERROR_PAUSE)
            self.link.drain()
            raise BackendRejected(error.kind, f"仪器没有开始：{error.text(self._script)}")
        if line != "r":
            self._synced = -1
            self.running = False
            raise StartUnknown(f"开始命令（r）回了意外的 {line!r}：不知道仪器开没开始")

    def stream(self, on_point: Callable[[Point], None]) -> Finish:
        """读脚本输出直到结束。链路断了、仪器太久没有输出抛 BackendError（之后要 recover）。"""
        segment, scan, aborted, error, notes = "", 0, False, None, []
        last = time.monotonic()
        try:
            while True:
                line = self.link.try_read_line(0.5)
                if line is None:
                    if time.monotonic() - last > self._silence:
                        self.lost = True
                        self.link.close()
                        raise BackendError(f"仪器 {self._silence:.0f} 秒没有输出：链路或仪器出了问题")
                    continue
                last = time.monotonic()
                if line == "":
                    break  # 脚本结束
                head = line[0]
                if head == "P":
                    try:
                        variables = ms.parse_package(line)
                    except ms.PackageError as exc:
                        notes.append(f"读坏的数据包被丢掉：{exc}")
                        continue
                    values: dict[str, float] = {}
                    status = 0
                    for variable in variables:
                        key = KEYS.get(variable.type)
                        if key is not None:
                            values[key] = float(variable.value)
                            status |= variable.status
                    on_point(Point(segment=segment, values=values, scan=scan, status=status))
                    continue
                if head == "M" and len(line) == 5:
                    try:
                        segment, scan = ms.TECHNIQUE_IDS.get(int(line[1:], 16), line), 0
                        continue
                    except ValueError:
                        pass
                if head == "C" and len(line) == 5 and line[1:].isdigit():
                    scan = int(line[1:])
                    continue
                if line in {"*", "-", "L", "+", "Y", "h", "H", "R"}:
                    continue
                if line == "Z":
                    aborted = True  # 仪器收到终止：测量循环收尾、on_finished 执行，然后空行
                    continue
                if line.startswith("Z!"):
                    continue  # 终止撞上了没有脚本在跑的空档（Z!0006）：不是测量出错
                if head == "T":
                    notes.append(line[1:])
                    continue
                error = ms.parse_error(line)
                if error is not None:
                    # 运行时出错：仪器之后可能还补一个空行；不等太久
                    try:
                        while self.link.try_read_line(1.0) not in (None, ""):
                            pass
                    except LinkError:
                        pass
                    break
                notes.append(f"不认识的输出：{line[:60]}")
        except LinkError as exc:
            self.lost = True
            raise BackendError(f"测量途中链路出错：{exc}", sent=True) from exc
        if error is not None:
            safe = self._cell_off()
            self.running = False
            return Finish("error", error=error.text(self._script), safe=safe, notes=notes)
        self.running = False
        return Finish("aborted" if aborted else "done", notes=notes)

    def _cell_off(self) -> bool:
        """运行时出错不执行 on_finished：另发一个只有 cell_off 的脚本，确认电池断开。"""
        try:
            time.sleep(ERROR_PAUSE)
            self.link.drain()
            self.link.write("e\ncell_off\n\n")
            deadline = time.monotonic() + self.timeout
            started = False
            while time.monotonic() < deadline:
                line = self.link.read_line(max(0.1, deadline - time.monotonic()))
                if ms.parse_error(line) is not None:
                    return False
                if line == "e":
                    started = True
                elif line == "" and started:
                    return True
        except LinkError:
            return False
        return False

    def abort(self) -> None:
        """发终止（`Z`），不等结果：读线程会看到 `Z` 和脚本结束。"""
        if not self.running or self.lost:
            return
        try:
            self.link.write("Z\n")
        except LinkError as exc:
            raise BackendError(f"终止命令没写出去：{exc}", sent=False) from exc

    def recover(self) -> None:
        """链路断过：重连、同步（停掉仪器上可能还在跑的脚本）。成功后仪器空闲、电池断开。"""
        self.link.close()
        self._synced = -1
        self._ensure()
        self.lost = False
        self.running = False

    def close(self) -> None:
        self.link.close()


def expected_points(plan: Plan) -> int | None:
    """这次测量大概有几个点（遥测里报进度用；算不准就 None）。"""
    s = plan.settings
    try:
        if plan.technique in {"ocp", "ca"}:
            return int(math.floor(float(s["duration_s"]) / float(s["interval_s"]) + 1e-9))
        if plan.technique == "lsv" and s.get("e_begin_V") != "ocp":
            return int(round(abs(float(s["e_end_V"]) - float(s["e_begin_V"])) / float(s["e_step_V"]))) + 1
        if plan.technique == "eis":
            return int(s["points"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return None
