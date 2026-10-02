"""真实接口：一块 IKA 磁力加热搅拌器（RCT digital、C-MAG HS 7 control、RET control-visc……）的 NAMUR 串口命令。

串口 9600 波特、7 数据位、偶校验、1 停止位（7E1），无流控；命令大写，以 CR LF 结尾，应答以 CR LF 结尾；
设备从不主动发数据。

    IN_NAME        型号，如 `RCT digital`
    IN_PV_1        外置温度探头（PT1000）℃       应答 `25.3 1`（数值 + 通道号）
    IN_PV_2        加热盘温度 ℃                   `80.1 2`
    IN_PV_4        实际转速 rpm                   `300 4`
    IN_SP_1        温度设定值                     IN_SP_4  转速设定值
    OUT_SP_1 x     设温度设定值      OUT_SP_4 x  设转速设定值          ← 写命令，不回
    START_1 / STOP_1   开 / 关加热    START_4 / STOP_4   开 / 关搅拌   ← 不回
    OUT_SP_12@t / OUT_SP_42@n   看门狗安全温度 / 安全转速（回显设的值）
    OUT_WD2@m      看门狗模式 2：m 秒（20–1500）内没再收到这条命令，设定值回落到安全值（回显 m）；OUT_WD2@0 关掉

模式 1（`OUT_WD1@`）触发后关加热关搅拌、要重新上电才能恢复，这里不用。

写命令没有应答，写出去不等于设备收到了。要确认，就紧跟一条读命令：同一条线、按顺序，读得到应答说明前面的写也到了
（`confirm`）。
"""
from __future__ import annotations

from .link import Link, LinkError

TEMP_EXTERNAL, TEMP_PLATE, SPEED = 1, 2, 4


class NamurError(Exception):
    """应答读不懂（不是数值、通道号对不上）。和 `LinkError` 一样按「这次没读到」处理。"""


def number(value: float) -> str:
    """设定值写成设备收的样子：整数不带小数点，其余保留一位小数。"""
    rounded = round(float(value), 1)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"


class Hotplate:
    def __init__(self, link: Link):
        self.link = link

    def describe(self) -> str:
        return self.link.describe()

    # ---------- 读 ----------

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

    def temperature(self, channel: int) -> float:
        return self._value(f"IN_PV_{channel}", channel)

    def speed(self) -> float:
        return self._value(f"IN_PV_{SPEED}", SPEED)

    def setpoint(self, channel: int) -> float:
        return self._value(f"IN_SP_{channel}", channel)

    # ---------- 写（设备不回） ----------

    def set_temperature(self, value: float) -> None:
        self.link.send(f"OUT_SP_1 {number(value)}")

    def set_speed(self, value: float) -> None:
        self.link.send(f"OUT_SP_4 {int(round(value))}")

    def start_heater(self) -> None:
        self.link.send("START_1")

    def stop_heater(self) -> None:
        self.link.send("STOP_1")

    def start_motor(self) -> None:
        self.link.send("START_4")

    def stop_motor(self) -> None:
        self.link.send("STOP_4")

    def confirm(self) -> float:
        """紧跟在写命令后面读一次转速：读得到，前面的写命令就已经到了设备（返回读到的转速）。"""
        return self.speed()

    # ---------- 看门狗（模式 2） ----------

    def _echo(self, command: str) -> str:
        reply = self.link.ask(command)
        if not reply:
            raise NamurError(f"{self.describe()} 对 {command} 没有回显")
        return reply

    def arm_watchdog(self, seconds: int, safe_temp: float, safe_speed: float) -> None:
        """设安全温度、安全转速，再打开看门狗模式 2：网关 `seconds` 秒内不再喂，设定值回落到安全值。"""
        self._echo(f"OUT_SP_12@{number(safe_temp)}")
        self._echo(f"OUT_SP_42@{int(round(safe_speed))}")
        self.feed_watchdog(seconds)

    def feed_watchdog(self, seconds: int) -> None:
        self._echo(f"OUT_WD2@{int(seconds)}")

    def clear_watchdog(self) -> None:
        self._echo("OUT_WD2@0")


__all__ = ["Hotplate", "LinkError", "NamurError", "SPEED", "TEMP_EXTERNAL", "TEMP_PLATE", "number"]
