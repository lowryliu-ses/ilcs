"""注射泵：Cavro DT（data terminal）ASCII 协议。Tecan Cavro XCalibur / XLP 用它，国产润泽（Runze）等注射泵兼容。

    发：/<地址><命令>R<CR>          例：/1I3R（阀转到 3 号口）、/1P1500R（吸 1500 步）、/1D1500R（推 1500 步）
    回：/0<状态字节>[数据]<ETX><CR><LF>

状态字节：第 5 位 = 1 表示就绪（空闲），低 4 位是错误码（0 没错）。动作命令一回就是「已收下」，之后发 `Q` 轮询到就绪。
查询命令（`Q`、`?` 读柱塞位置）不带 `R`。`T` 立即终止当前动作。

`transfer` 把一段体积从源端口吸进来、转到出液口推出去：阀先转到源端口再吸、转到出液口再推，柱塞平时停在 0 位。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .errors import DeviceError
from .link import Link, LinkError

ERRORS = {
    1: "初始化失败", 2: "命令无效", 3: "参数无效", 6: "EEPROM 故障", 7: "泵没初始化", 9: "柱塞过载（堵塞或阀没到位）",
    10: "阀过载", 11: "阀在当前位置不允许柱塞动作", 15: "命令溢出",
}


class PumpError(DeviceError):
    """泵明确报错。"""

    def __init__(self, code: int, during: str):
        super().__init__(f"注射泵{during}报错 {code}：{ERRORS.get(code, '未知错误')}")
        self.code = code


class CavroPump:
    def __init__(self, liquid: dict[str, Any]):
        self.link = Link(liquid["link"], timeout=float(liquid.get("timeout_sec") or 3), read_terminator=b"\n",
                         write_terminator=b"\r")
        self.address = str(liquid.get("address") or 1)
        self.syringe_ul = float(liquid["syringe_ul"])
        self.steps = int(liquid["steps"])
        self.move_timeout = float(liquid.get("move_timeout_sec") or 60)

    # ---------- 帧 ----------

    def _ask(self, command: str) -> tuple[bool, int, str]:
        reply = self.link.ask(f"/{self.address}{command}")
        if not reply.startswith("/0") or len(reply) < 3:
            raise LinkError(f"注射泵应答格式不对：{reply!r}", sent=True)
        status = ord(reply[2])
        return bool(status & 0x20), status & 0x0F, reply[3:]

    def _run(self, command: str, during: str, cancel: threading.Event | None = None) -> None:
        """发一条动作命令并等它做完。泵报错抛 PumpError；收到终止信号先发 T 再抛 InterruptedError。"""
        ready, error, _ = self._ask(command + "R")
        if error:
            raise PumpError(error, during)
        deadline = time.monotonic() + self.move_timeout
        while True:
            ready, error, _ = self._ask("Q")
            if error:
                raise PumpError(error, during)
            if ready:
                return
            if cancel is not None and cancel.is_set():
                self.stop()
                raise InterruptedError(f"{during}时被终止")
            if time.monotonic() > deadline:
                raise LinkError(f"注射泵{during}超过 {self.move_timeout:.0f} 秒没做完", sent=True)
            time.sleep(0.05)

    # ---------- 动作 ----------

    def check_ready(self) -> None:
        """动作之前读状态。没初始化（错误 7）不自己初始化：初始化时柱塞回 0 位，会把注射器里的残液从当前阀口推出去，
        可能推进秤上的样品瓶、又在去皮之前，天平算不进去——交现场在对着废液口时初始化。其他错误照实抛出，设备不动。"""
        ready, error, _ = self._ask("Q")
        if error == 7:
            raise PumpError(7, "自检时（请在现场把阀对着废液口初始化一次）")
        if error:
            raise PumpError(error, "自检时")
        if not ready:
            raise PumpError(15, "自检时（泵还在动作）")

    def position(self) -> int:
        _, error, data = self._ask("?")
        if error:
            raise PumpError(error, "读柱塞位置时")
        return int(data or 0)

    def transfer(self, source: int, output: int, volume_ul: float, cancel: threading.Event | None = None) -> int:
        """从 source 口吸 volume_ul、从 output 口推出；返回实际走的步数（按柱塞分辨率取整）。"""
        steps = max(1, round(volume_ul / self.syringe_ul * self.steps))
        steps = min(steps, self.steps)
        self._run(f"I{source}", f"阀转到 {source} 号口", cancel)
        self._run(f"P{steps}", "吸液", cancel)
        self._run(f"I{output}", f"阀转到出液口 {output}", cancel)
        self._run(f"D{steps}", "推液", cancel)
        return steps

    def stop(self) -> None:
        try:
            self._ask("T")
        except LinkError:
            pass
