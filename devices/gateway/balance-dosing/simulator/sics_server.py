"""假天平：TCP 上说 MT-SICS，秤盘在 `World` 里；带 Quantos 的再多一组 QRD / QRA 命令（加粉、加样头、前门）。

- 天平：`@`、`I2`、`I3`、`I4`、`S`（等稳定，最多 `stable_wait_sec`，等不到回 `S I`）、`SI`（`S S` / `S D`）、
  `T`、`TA`、`TAC`、`Z`；不认识的命令回 `ES`；超过量程回 `S +`。
- Quantos：`QRD 1 1 5 <mg>` 目标、`… 6` 容差、`… 7` 方式、`… 8` 样品号；`QRD 2 3 7` 前门（2 关 3 开）；
  `QRD 2 4 11` / `QRD 2 4 12` 加样头 / 加粉结果（`B`、XML 行、`A`）；`QRA 60 7 <2|3>` 关 / 开门、
  `QRA 60 2 <4|3>` 锁 / 松加样头（`B` 再 `A`）；`QRA 61 1` 开始加粉先回 `B`，`dose_seconds` 后在同一连接上回
  `QRA 61 1 A`（或出错 `QRA 61 1 I <代码>`）；`QRA 61 4` 停止（回 `QRA 61 4 A`，进行中的加粉回 `I 8`）。
- `powder_flow_error = True`：下一次加粉加到一半回 `I 7`。
"""
from __future__ import annotations

import socketserver
import threading
import time
from typing import Callable

from .world import World

Send = Callable[[str], None]


class FakeScale:
    def __init__(self, world: World, *, serial: str = "ILCS-SIMULATOR-B000001", model: str = "XPE206DRQ",
                 quantos: bool = True, dose_seconds: float = 0.5, accuracy_pct: float = 0.6,
                 stable_wait_sec: float = 3.0, weigh_seconds: float = 0.0):
        self.world = world
        self.serial, self.model, self.quantos = serial, model, quantos
        self.dose_seconds, self.accuracy_pct, self.stable_wait = dose_seconds, accuracy_pct, stable_wait_sec
        self.weigh_seconds = weigh_seconds  # 稳定读数至少要这么久（真天平给 S 的结果要一两秒）
        self.settings: dict[str, str] = {}
        self.pins_locked = False
        self.result: dict[str, float | str] | None = None
        self.dosing: dict | None = None
        self.powder_flow_error = False
        self.lock = threading.RLock()

    # ---------- 天平 ----------

    def _weight(self, status: str, value: float) -> str:
        return f"{status} {value:11.5f} g"

    def _wait_stable(self) -> bool:
        deadline = time.monotonic() + self.stable_wait
        while not self.world.stable():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def handle(self, line: str, send: Send) -> None:
        tokens = line.split()
        if not tokens:
            return
        head = tokens[0]
        if head in {"QRD", "QRA"}:
            if not self.quantos:
                send("ES")
            else:
                self._quantos(tokens, send)
            return
        over = self.world.pan_g > self.world.capacity_g
        if line == "@":
            send(f'I4 A "{self.serial}"')
        elif line == "I2":
            send(f'I2 A "{self.model} {self.world.capacity_g:.4f} g"')
        elif line == "I3":
            send('I3 A "1.10 9.9.9 ILCS-SIMULATOR"')
        elif line == "I4":
            send(f'I4 A "{self.serial}"')
        elif line == "S":
            time.sleep(self.weigh_seconds)
            if over:
                send("S +")
            elif not self._wait_stable():
                send("S I")
            else:
                send("S S" + self._weight("", self.world.net())[0:])
        elif line == "SI":
            send("S +" if over else ("S S" if self.world.stable() else "S D") + self._weight("", self.world.net()))
        elif line == "T":
            if over:
                send("T +")
            elif not self._wait_stable():
                send("T I")
            else:
                send("T S" + self._weight("", self.world.tare()))
        elif line == "TA":
            send("TA A" + self._weight("", self.world.tare_g))
        elif line == "TAC":
            self.world.tare_g = 0.0
            send("TAC A")
        elif line == "Z":
            if not self._wait_stable():
                send("Z I")
            else:
                self.world.zero()
                send("Z A")
        else:
            send("ES")

    # ---------- Quantos ----------

    def _xml(self, path: str, body: list[str], send: Send) -> None:
        send(f"{path} B")
        for row in body:
            send(row)
        send(f"{path} A")

    def _quantos(self, tokens: list[str], send: Send) -> None:
        with self.lock:
            if tokens[:3] == ["QRD", "1", "1"] and len(tokens) >= 5:
                self.settings[tokens[3]] = " ".join(tokens[4:])
                send(" ".join(tokens[:4]) + " A")
                return
            path = " ".join(tokens[:4])
            if path == "QRD 2 3 7":
                send(f"{path} {3 if self.world.door_open else 2} A")
            elif path == "QRD 2 2 9":
                send(f"{path} {1 if self.world.net() > 0.0005 else 0} A")
            elif path == "QRD 2 4 11":
                head = self.world.head()
                if head is None:
                    send(f"{path} I 1")
                    return
                limit = head.remaining_doses + 1000
                self._xml(path, [
                    "<Info_head>", f"<Substance>{head.substance}</Substance>", f"<Lot_ID>{head.lot}</Lot_ID>",
                    f"<Dose_limit>{limit}</Dose_limit>", f"<Dosing_counter>{1000}</Dosing_counter>",
                    f'<Rem._quantity Unit="mg">{head.content_g * 1000:.1f}</Rem._quantity>',
                    "<Exp._date>2027-12-31</Exp._date>", "</Info_head>",
                ], send)
            elif path == "QRD 2 4 12":
                if self.result is None:
                    send(f"{path} I 4")
                    return
                self._xml(path, [
                    "<Dosing>", f"<Substance>{self.result['substance']}</Substance>",
                    f'<Target_quantity Unit="mg">{self.result["target_mg"]:.3f}</Target_quantity>',
                    f'<Content Unit="mg">{self.result["content_mg"]:.3f}</Content>', "</Dosing>",
                ], send)
            elif tokens[:3] == ["QRA", "60", "7"] and len(tokens) == 4:
                self.world.door_open = tokens[3] == "3"
                send("QRA 60 7 B")
                send("QRA 60 7 A")
            elif tokens[:3] == ["QRA", "60", "2"] and len(tokens) == 4:
                if self.world.head() is None:
                    send("QRA 60 2 I 1")
                    return
                self.pins_locked = tokens[3] == "4"
                send("QRA 60 2 B")
                send("QRA 60 2 A")
            elif tokens == ["QRA", "61", "1"]:
                self._start_dose(send)
            elif tokens == ["QRA", "61", "4"]:
                self._stop_dose(send)
            else:
                send(" ".join(tokens[:4]) + " L")

    def _start_dose(self, send: Send) -> None:
        head = self.world.head()
        target = float(self.settings.get("5", "0") or 0)
        if head is None:
            send("QRA 61 1 I 1")
        elif self.dosing is not None:
            send("QRA 61 1 I 2")
        elif self.world.door_open or not self.pins_locked or target <= 0:
            send("QRA 61 1 I 5")
        elif head.remaining_doses <= 0:
            send("QRA 61 1 I 11")
        else:
            send("QRA 61 1 B")
            error = (self.world.random.random() * 2 - 1) * self.accuracy_pct / 100
            dosing = {"target_mg": target, "content_mg": target * (1 + error), "substance": head.substance,
                      "started": time.monotonic(), "send": send, "flow_error": self.powder_flow_error}
            self.powder_flow_error = False
            dosing["timer"] = threading.Timer(self.dose_seconds * (0.5 if dosing["flow_error"] else 1),
                                              self._finish_dose, args=(dosing,))
            self.dosing = dosing
            dosing["timer"].start()

    def _finish_dose(self, dosing: dict, *, stopped: bool = False) -> None:
        with self.lock:
            if self.dosing is not dosing:
                return
            self.dosing = None
            fraction = 1.0
            if stopped or dosing["flow_error"]:
                fraction = min(1.0, (time.monotonic() - dosing["started"]) / max(self.dose_seconds, 1e-6)) * 0.8
            content = dosing["content_mg"] * fraction
            self.world.add(content / 1000)
            head = self.world.head()
            if head is not None:
                head.remaining_doses -= 1
                head.content_g -= content / 1000
            self.result = {"substance": dosing["substance"], "target_mg": dosing["target_mg"], "content_mg": content}
        if stopped:
            dosing["send"]("QRA 61 1 I 8")
        elif dosing["flow_error"]:
            dosing["send"]("QRA 61 1 I 7")
        else:
            dosing["send"]("QRA 61 1 A")

    def _stop_dose(self, send: Send) -> None:
        dosing = self.dosing
        send("QRA 61 4 A")
        if dosing is not None:
            dosing["timer"].cancel()
            self._finish_dose(dosing, stopped=True)


class ScaleServer:
    def __init__(self, scale: FakeScale, host: str = "127.0.0.1", port: int = 0):
        self.scale = scale
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                lock = threading.Lock()

                def send(text: str) -> None:
                    with lock:
                        try:
                            self.request.sendall(text.encode("utf-8") + b"\r\n")
                        except OSError:
                            pass

                buffer = b""
                while True:
                    try:
                        chunk = self.request.recv(1024)
                    except OSError:
                        return
                    if not chunk:
                        return
                    buffer += chunk
                    while b"\r\n" in buffer:
                        raw, buffer = buffer.split(b"\r\n", 1)
                        line = raw.decode("utf-8", errors="replace").strip()
                        if line:
                            owner.scale.handle(line, send)

        self.server = socketserver.ThreadingTCPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
