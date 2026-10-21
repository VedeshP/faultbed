"""Control-plane connections to nodes, and the clock the orchestrator runs on.

A NodePort speaks the newline-JSON protocol. SerialNodePort talks to a real
board over USB; sim.SimNodePort talks to a simulated one. The orchestrator
cannot tell the difference, which is what lets the whole software stack be
developed and tested without hardware.
"""
from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, List, Optional

EventCallback = Callable[["NodePort", dict, int], None]  # (port, event, host_time_ns)


class Clock(ABC):
    @abstractmethod
    def now_ns(self) -> int: ...

    @abstractmethod
    def sleep(self, seconds: float) -> None: ...

    def now(self) -> float:
        return self.now_ns() / 1e9

    def wait_until(self, pred: Callable[[], bool], timeout: float, step: float = 0.005) -> bool:
        deadline = self.now() + timeout
        while not pred():
            if self.now() >= deadline:
                return False
            self.sleep(step)
        return True


class RealClock(Clock):
    def now_ns(self) -> int:
        return time.perf_counter_ns()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class NodePort(ABC):
    name: str = "?"

    @abstractmethod
    def open(self, on_event: EventCallback) -> None: ...

    @abstractmethod
    def send(self, cmd: dict) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class SerialNodePort(NodePort):
    def __init__(self, device: str, baud: int = 921600, clock: Optional[Clock] = None):
        self.name = device
        self.device = device
        self.baud = baud
        self.clock = clock or RealClock()
        self._ser = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wlock = threading.Lock()
        self.raw_tail: List[str] = []  # non-JSON output (boot messages, panics) for debugging

    def open(self, on_event: EventCallback) -> None:
        import serial  # pyserial

        # A plain device name (COM5, /dev/ttyUSB0) or a pyserial URL, e.g. rfc2217://localhost:4000 for a board
        # simulated in Wokwi. Either way the node speaks the same protocol, so real and virtual boards can be mixed.
        s = serial.serial_for_url(self.device, do_not_open=True)
        s.baudrate = self.baud
        s.timeout = 0.05
        s.dtr = False  # don't reset the ESP32 on open where the adapter allows it
        s.rts = False
        s.open()
        self._ser = s
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, args=(on_event,), daemon=True, name=f"serial-{self.device}")
        self._thread.start()

    def _reader(self, on_event: EventCallback) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(4096)
            except Exception:  # noqa: BLE001 - board unplugged
                break
            if not chunk:
                continue
            t = self.clock.now_ns()
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if text.startswith("{"):
                    try:
                        on_event(self, json.loads(text), t)
                        continue
                    except json.JSONDecodeError:
                        pass
                self.raw_tail = (self.raw_tail + [text])[-50:]

    def send(self, cmd: dict) -> None:
        data = (json.dumps(cmd, separators=(",", ":")) + "\n").encode()
        with self._wlock:
            self._ser.write(data)
            self._ser.flush()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        if self._ser:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass


def list_serial_devices() -> List[str]:
    """USB serial devices that look like ESP32 dev boards (CP210x, CH340, FTDI, native USB)."""
    from serial.tools import list_ports

    likely = ("CP210", "CH340", "CH910", "USB Serial", "USB-SERIAL", "UART", "JTAG", "Espressif", "FT232", "USB")
    out = []
    for p in list_ports.comports():
        desc = f"{p.description} {p.manufacturer or ''} {p.hwid}"
        if any(k.lower() in desc.lower() for k in likely):
            out.append(p.device)
    return sorted(out)
