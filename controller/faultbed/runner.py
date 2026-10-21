"""Experiment orchestration: discover boards, configure, run the timeline, collect logs.

Run directory layout (results/<timestamp>_<name>/<variant>/rep<k>/):
    scenario.yaml   resolved scenario this run used
    meta.json       boards, firmware, seed, host clock sync, timeline actually executed
    events.jsonl    every node event: {...event, "node": node_id, "host": host_ns (serial arrival)}
    metrics.json    computed by metrics.analyze()
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import platform
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import metrics as M
from .ports import Clock, NodePort, RealClock, SerialNodePort, list_serial_devices
from .scenario import Scenario, dump_yaml, parse_link

SYNC_SAMPLES = 16


class RunError(RuntimeError):
    pass


@dataclass
class Board:
    port: NodePort
    mac: str = ""
    chip: str = ""
    fw: str = ""
    transports: List[str] = field(default_factory=list)
    node_id: int = 0
    ip: str = ""
    link_up: bool = False
    online: bool = False


class Testbed:
    """The set of connected boards plus the event plumbing shared by all runs."""

    def __init__(self, clock: Optional[Clock] = None):
        self.clock = clock or RealClock()
        self.boards: Dict[str, Board] = {}  # by port name
        self._lock = threading.Lock()
        self._acks: Dict[tuple, dict] = {}
        self._sync: Dict[str, List[tuple]] = {}
        self._sync_sent: Dict[tuple, int] = {}
        self._recording: Optional[List[dict]] = None
        self.listeners: List[Callable[[Board, dict, int], None]] = []

    # ---- connection management ----
    def attach(self, ports: List[NodePort]) -> None:
        for p in ports:
            self.boards[p.name] = Board(port=p)
            p.open(self._on_event)

    def attach_serial(self, devices: Optional[List[str]] = None, baud: int = 921600) -> None:
        devices = devices if devices is not None else list_serial_devices()
        self.attach([SerialNodePort(d, baud, self.clock) for d in devices])

    def detach_all(self) -> None:
        for b in self.boards.values():
            b.port.close()
        self.boards.clear()

    def discover(self, timeout: float = 3.0) -> List[Board]:
        for b in self.boards.values():
            b.online = False
        end = self.clock.now() + timeout
        while self.clock.now() < end and not all(b.online for b in self.boards.values()):
            for b in self.boards.values():
                if not b.online:
                    b.port.send({"c": "hello"})
            self.clock.wait_until(lambda: all(b.online for b in self.boards.values()), 0.6)
        return [b for b in self.boards.values() if b.online]

    def identify(self, port_name: str, ms: int = 3000) -> None:
        self.boards[port_name].port.send({"c": "led", "ms": ms})

    # ---- event plumbing ----
    def _on_event(self, port: NodePort, ev: dict, host_ns: int) -> None:
        b = self.boards.get(port.name)
        if b is None:
            return
        e = ev.get("e")
        if e == "hello":
            b.mac, b.chip, b.fw = ev.get("mac", ""), ev.get("chip", ""), ev.get("fw", "")
            b.transports = list(ev.get("transports", []))
            b.online = True
        elif e == "link":
            b.link_up = bool(ev.get("up"))
            if ev.get("ip"):
                b.ip = ev["ip"]
        elif e == "ack":
            with self._lock:
                self._acks[(port.name, ev.get("c"))] = ev
        elif e == "sync":
            sent = self._sync_sent.pop((port.name, ev.get("h")), None)
            if sent is not None:
                self._sync.setdefault(port.name, []).append((sent, host_ns, int(ev["t"])))
        rec = self._recording
        if rec is not None and b.node_id:
            rec.append({**ev, "node": b.node_id, "host": host_ns})
        for fn in list(self.listeners):
            try:
                fn(b, ev, host_ns)
            except Exception:  # noqa: BLE001 - a broken UI listener must never kill a run
                pass

    def command(self, b: Board, cmd: dict, wait_ack: bool = True, timeout: float = 5.0) -> dict:
        key = (b.port.name, cmd["c"])
        with self._lock:
            self._acks.pop(key, None)
        b.port.send(cmd)
        if not wait_ack:
            return {}
        if not self.clock.wait_until(lambda: key in self._acks, timeout):
            raise RunError(f"node {b.node_id or b.port.name}: no ack for '{cmd['c']}'")
        ack = self._acks[key]
        if not ack.get("ok"):
            raise RunError(f"node {b.node_id}: '{cmd['c']}' failed: {ack.get('err', '?')}")
        return ack

    def sync_clocks(self, boards: List[Board], samples: int = SYNC_SAMPLES) -> None:
        """Cristian-style exchange over the serial link; samples are filtered at analysis time."""
        for k in range(samples):
            for b in boards:
                h = (k + 1) * 1000 + b.node_id
                self._sync_sent[(b.port.name, h)] = self.clock.now_ns()
                b.port.send({"c": "sync", "h": h})
            self.clock.sleep(0.02)
        self.clock.sleep(0.1)


def assign_boards(s: Scenario, online: List[Board]) -> Dict[int, Board]:
    """Scenario node id -> physical board. Pinned by MAC or port, otherwise by sorted MAC (stable across sessions)."""
    free = sorted(online, key=lambda b: b.mac)
    out: Dict[int, Board] = {}
    for spec in s.nodes:
        pick = None
        if spec.mac:
            pick = next((b for b in free if b.mac.lower() == spec.mac.lower()), None)
        elif spec.port:
            pick = next((b for b in free if b.port.name == spec.port), None)
        if spec.mac or spec.port:
            if pick is None:
                raise RunError(f"node {spec.id}: board {spec.mac or spec.port} not connected")
            out[spec.id] = pick
            free.remove(pick)
    for spec in s.nodes:
        if spec.id in out:
            continue
        if not free:
            raise RunError(f"scenario needs {len(s.nodes)} boards, only {len(online)} online")
        out[spec.id] = free.pop(0)
    return out


@dataclass
class Progress:
    phase: str = "idle"
    variant: str = ""
    rep: int = 0
    reps: int = 0
    run_index: int = 0
    run_total: int = 0
    elapsed_s: float = 0.0
    duration_s: float = 0.0
    run_dir: str = ""
    error: str = ""


class Runner:
    def __init__(self, testbed: Testbed, results_root: str | pathlib.Path = "results"):
        self.tb = testbed
        self.clock = testbed.clock
        self.results_root = pathlib.Path(results_root)
        self.progress = Progress()
        self.stop_requested = threading.Event()
        self.on_progress: Optional[Callable[[Progress], None]] = None

    def _phase(self, phase: str, **kw) -> None:
        self.progress.phase = phase
        for k, v in kw.items():
            setattr(self.progress, k, v)
        if self.on_progress:
            self.on_progress(self.progress)

    # ---- campaign = sweep variants x repeats ----
    def run_campaign(self, s: Scenario) -> pathlib.Path:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        root = self.results_root / f"{stamp}_{_slug(s.name)}"
        root.mkdir(parents=True, exist_ok=True)
        (root / "campaign.yaml").write_text(dump_yaml(s), encoding="utf-8")
        variants = s.expand()
        total = len(variants) * s.repeats
        self.stop_requested.clear()
        k = 0
        try:
            for vi, v in enumerate(variants):
                vdir = root / (f"v{vi:02d}_{_slug(v.name)}" if len(variants) > 1 else "v00")
                for rep in range(v.repeats):
                    if self.stop_requested.is_set():
                        break
                    k += 1
                    seed = (v.seed + rep) if v.vary_seed else v.seed
                    self._phase("starting", variant=v.name, rep=rep + 1, reps=v.repeats, run_index=k, run_total=total)
                    self.run_once(v, vdir / f"rep{rep + 1:02d}", seed)
            M.summarize_campaign(root)
            self._phase("done", run_dir=str(root))
        except Exception as e:
            self._phase("error", error=str(e), run_dir=str(root))
            raise
        return root

    def run_once(self, s: Scenario, run_dir: pathlib.Path, seed: int) -> dict:
        tb, clk = self.tb, self.clock
        run_dir.mkdir(parents=True, exist_ok=True)
        self._phase("discovering", run_dir=str(run_dir), duration_s=s.duration_s, elapsed_s=0.0)
        online = tb.discover()
        bmap = assign_boards(s, online)
        for b in tb.boards.values():
            b.node_id = 0
        for nid, b in bmap.items():
            b.node_id = nid
            b.link_up = False
            b.ip = ""
            if s.transport.name not in b.transports:
                raise RunError(f"node {nid} ({b.mac}) firmware has no '{s.transport.name}' transport")
        boards = list(bmap.values())

        events: List[dict] = []
        tb._recording = events
        timeline_log: List[dict] = []
        meta = {
            "scenario": s.name, "seed": str(seed), "transport": s.transport.name, "started": dt.datetime.now().isoformat(),
            "host": platform.node(), "python": platform.python_version(),
            "boards": {str(nid): {"port": b.port.name, "mac": b.mac, "chip": b.chip, "fw": b.fw} for nid, b in bmap.items()},
        }
        try:
            self._phase("configuring")
            for nid, b in bmap.items():
                tb.command(b, s.node_config(nid, seed), timeout=10)

            self._phase("linking")
            if not clk.wait_until(lambda: all(b.link_up for b in boards), 45):
                down = [b.node_id for b in boards if not b.link_up]
                raise RunError(f"transport '{s.transport.name}' did not come up on nodes {down} "
                               "(check Wi-Fi credentials / broker address)")
            peers = {str(b.node_id): {"mac": b.mac, "ip": b.ip} for b in boards}
            for b in boards:
                tb.command(b, {"c": "peers", "peers": peers})
            clk.sleep(0.5)  # let MQTT subscriptions / ESP-NOW peers settle

            self._phase("syncing")
            tb.sync_clocks(boards)

            self._phase("running")
            for b in boards:
                tb.command(b, {"c": "start"}, wait_ack=False)
            t0 = clk.now()
            meta["t0_host_ns"] = int(t0 * 1e9)
            pending = sorted(s.timeline, key=lambda e: e.at_s)
            while True:
                el = clk.now() - t0
                self.progress.elapsed_s = el
                while pending and pending[0].at_s <= el:
                    ev = pending.pop(0)
                    timeline_log.append(self._apply_timeline(ev, bmap, clk.now_ns()))
                if el >= s.duration_s or self.stop_requested.is_set():
                    break
                clk.sleep(min(0.01, max(0.001, s.duration_s - el)))
            for b in boards:
                tb.command(b, {"c": "stop"}, wait_ack=False)
            meta["t_stop_host_ns"] = clk.now_ns()

            self._phase("draining")
            clk.sleep(s.drain_s)
            tb.sync_clocks(boards)
        finally:
            tb._recording = None
            meta["sync"] = {str(b.node_id): tb._sync.pop(b.port.name, []) for b in boards}
            meta["timeline"] = timeline_log
            meta["stopped_early"] = self.stop_requested.is_set()
            (run_dir / "scenario.yaml").write_text(dump_yaml(s), encoding="utf-8")
            (run_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            with open(run_dir / "events.jsonl", "w", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(e, separators=(",", ":")) + "\n")

        self._phase("analyzing")
        result = M.analyze(run_dir)
        self._phase("finished")
        return result

    def _apply_timeline(self, ev, bmap: Dict[int, Board], host_ns: int) -> dict:
        tb = self.tb
        rec = {"at_s": ev.at_s, "action": ev.action, "host_ns": host_ns}
        if ev.action == "kill":
            tb.command(bmap[ev.node], {"c": "kill", "hard": ev.hard}, wait_ack=False)
            rec.update(node=ev.node, hard=ev.hard)
        elif ev.action == "revive":
            tb.command(bmap[ev.node], {"c": "revive"}, wait_ack=False)
            rec.update(node=ev.node)
        else:
            rule = ev.rule.model_dump() if (ev.action == "fault" and ev.rule) else None
            if ev.link == "default":
                for b in bmap.values():
                    tb.command(b, {"c": "fault", "src": -1, "rule": rule}, wait_ack=False)
            else:
                a, dst = parse_link(ev.link)
                tb.command(bmap[dst], {"c": "fault", "src": a, "rule": rule}, wait_ack=False)
            rec.update(link=ev.link, rule=rule)
        return rec


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.=-]+", "_", s).strip("_")[:80] or "run"
