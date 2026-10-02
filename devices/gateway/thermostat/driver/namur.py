"""真实接口：一块 IKA 磁力搅拌板（RCT digital、C-MAG HS 7 control……）的 NAMUR 命令，只用电机。

照 ika-stirrer 的 `driver/namur.py` 取了搅拌那一半。板子放在冷块上只管搅：**不发 `START_1`**（不开加热），
停的时候 `STOP_4` 之后顺手发一条 `STOP_1`（本来就没开，万一有人在面板上开了也关掉，冷块上不能有热源）。

串口 9600 波特、7 数据位、偶校验、1 停止位（7E1），无流控；命令大写，以 CR LF 结尾，应答以 CR LF 结尾。

    IN_NAME        型号，如 `RCT digital`
    IN_PV_4        实际转速 rpm          应答 `300 4`（数值 + 通道号）
    IN_SP_4        转速设定值
    OUT_SP_4 n     设转速设定值           ← 写命令，不回
    START_4 / STOP_4   开 / 关搅拌         ← 不回
    STOP_1         关加热                 ← 不回

写命令没有应答，写出去不等于设备收到了：紧跟一条读命令（同一条线、按顺序），读得到应答说明前面的写也到了（`confirm`）。
"""
from __future__ import annotations

from .link import Link, LinkError

SPEED = 4


class NamurError(Exception):
    """应答读不懂（不是数值、通道号对不上）。和 `LinkError` 一样按「这次没读到」处理。"""


class Stirrer:
    def __init__(self, link: Link):
        self.link = link

    def describe(self) -> str:
        return self.link.describe()

    def _value(self, command: str, channel: int) -> float:
        reply = self.link.ask(command)
        parts = reply.split()
        try:
            value = float(parts[0])
        except (IndexError, ValueError) as exc:
            raise NamurError(f"{self.describe()} 对 {command} 回 {reply!r}，不是数值") from exc
        # 应答带通道号的就核对：错位的应答（上一条晚到的）不能当成这一条的
        if len(parts) > 1 and parts[1] != str(channel):
            raise NamurError(f"{self.describe()} 对 {command} 回 {reply!r}，通道号不是 {channel}")
        return value

    def name(self) -> str:
        reply = self.link.ask("IN_NAME")
        if not reply:
            raise NamurError(f"{self.describe()} 对 IN_NAME 回了空行")
        return reply

    def speed(self) -> float:
        return self._value(f"IN_PV_{SPEED}", SPEED)

    def speed_setpoint(self) -> float:
        return self._value(f"IN_SP_{SPEED}", SPEED)

    def set_speed(self, rpm: float) -> None:
        self.link.send(f"OUT_SP_{SPEED} {int(round(rpm))}")

    def start(self) -> None:
        self.link.send(f"START_{SPEED}")

    def stop(self) -> None:
        """关搅拌，再关加热（加热本来不开）。只发不读：要确认就接着 `confirm()`。"""
        self.link.send(f"STOP_{SPEED}")
        self.link.send("STOP_1")

    def confirm(self) -> float:
        """紧跟在写命令后面读一次转速：读得到，前面的写命令就已经到了设备（返回读到的转速）。"""
        return self.speed()


__all__ = ["LinkError", "NamurError", "SPEED", "Stirrer"]
