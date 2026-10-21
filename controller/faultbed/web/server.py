"""Web UI backend: REST for configuration/results, WebSocket for the live view."""
from __future__ import annotations

import asyncio
import collections
import json
import pathlib
import re
import threading
from typing import Any, Dict, List, Optional

import yaml
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .. import __version__
from .. import topology as topo
from ..runner import Board, Progress, Runner, RunError, Testbed
from ..scenario import Scenario, dump_yaml

STATIC = pathlib.Path(__file__).parent / "static"
SAFE = re.compile(r"^[A-Za-z0-9_.=,\[\]+-]+$")

# Parameters the UI renders as forms. Transports not listed here still work: the UI falls back to a JSON editor.
TRANSPORT_PARAMS = {
    "espnow": [{"key": "channel", "type": "int", "default": 1, "help": "Wi-Fi channel 1-13"},
               {"key": "unicast", "type": "bool", "default": True, "help": "unicast = MAC ACK + retries"}],
    "udp": [{"key": "port", "type": "int", "default": 4210}],
    "mqtt": [{"key": "host", "type": "str", "default": "192.168.1.10", "help": "broker IP (this PC)"},
             {"key": "port", "type": "int", "default": 1883},
             {"key": "keepalive_s", "type": "int", "default": 5}],
    "coap": [{"key": "confirmable", "type": "bool", "default": True, "help": "CON (ACK + retransmit) vs NON"},
             {"key": "ack_timeout_ms", "type": "int", "default": 2000},
             {"key": "max_retransmit", "type": "int", "default": 4},
             {"key": "port", "type": "int", "default": 5683}],
}
WIFI_TRANSPORTS = {"udp", "mqtt", "coap"}


class Live:
    """Rolling per-flow / per-node counters for the live view (fed from the event stream)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset(None)

    def reset(self, s: Optional[Scenario]) -> None:
        with self.lock:
            self.flows: Dict[int, dict] = {}
            self.by_key: Dict[tuple, int] = {}
            if s:
                for f in s.flows:
                    self.flows[f.id] = {"id": f.id, "src": f.src, "dst": f.dst, "sent": 0, "delivered": 0, "seen": set(),
                                        "rtt": collections.deque(maxlen=300), "last_rtt": None}
            self.nodes: Dict[int, dict] = {}
            self.log = collections.deque(maxlen=300)
            self.seq = 0

    def on_event(self, b: Board, ev: dict, host_ns: int) -> None:
        nid = b.node_id
        if not nid:
            return
        e = ev.get("e")
        with self.lock:
            n = self.nodes.setdefault(nid, {"id": nid, "down": False, "link": False, "arr": 0, "inj": 0})
            if e == "tx":
                f = self.flows.get(int(ev.get("f", -1)))
                if f and f["src"] == nid:
                    f["sent"] += 1
            elif e == "rx":
                f = self.flows.get(int(ev.get("f", -1)))
                if f and f["dst"] == nid and f["src"] == int(ev.get("o", -1)) and ev.get("s") not in f["seen"]:
                    f["seen"].add(ev.get("s"))  # unique sequence numbers, duplicates don't count
                    f["delivered"] += 1
            elif e == "rtt":
                f = self.flows.get(int(ev.get("f", -1)))
                if f and f["src"] == nid:
                    v = int(ev["rtt"]) / 1000.0
                    f["rtt"].append([host_ns / 1e9, v])
                    f["last_rtt"] = v
            elif e == "arr":
                n["arr"] += 1
                if ev.get("v") in ("loss", "cor"):
                    n["inj"] += 1
            elif e in ("down", "up"):
                n["down"] = e == "down"
            elif e == "link":
                n["link"] = bool(ev.get("up"))
            if e not in ("tx", "rx", "arr", "fwd", "rtt", "etx", "sync"):
                self.seq += 1
                self.log.append({"k": self.seq, "node": nid, **ev})

    def snapshot(self, since: int) -> dict:
        with self.lock:
            flows = []
            for f in self.flows.values():
                d = {k: v for k, v in f.items() if k != "seen"}
                d["rtt"] = list(f["rtt"])[-120:]
                d["pdr"] = f["delivered"] / f["sent"] if f["sent"] else None
                flows.append(d)
            return {"flows": flows, "nodes": list(self.nodes.values()),
                    "log": [x for x in self.log if x["k"] > since]}


class App:
    def __init__(self, tb: Testbed, results: pathlib.Path, scenarios: pathlib.Path, mode: str):
        self.tb = tb
        self.results = results
        self.scenarios = scenarios
        self.mode = mode
        self.runner = Runner(tb, results)
        self.live = Live()
        self.busy = threading.Lock()  # one run / scan at a time (also protects the simulator's clock)
        self.thread: Optional[threading.Thread] = None
        self.last_error = ""
        self._current: Optional[Scenario] = None
        tb.listeners.append(self.live.on_event)
        self.runner.on_progress = self._on_progress

    def _on_progress(self, p: Progress) -> None:
        if p.phase == "configuring":
            self.live.reset(self._current)

    def boards(self) -> List[dict]:
        return [{"port": b.port.name, "mac": b.mac, "chip": b.chip, "fw": b.fw, "transports": b.transports,
                 "online": b.online, "node_id": b.node_id, "link_up": b.link_up, "ip": b.ip}
                for b in self.tb.boards.values()]

    def transports(self) -> List[str]:
        names = sorted({t for b in self.tb.boards.values() for t in b.transports}) or list(TRANSPORT_PARAMS)
        return names

    def scan(self) -> None:
        if not self.busy.acquire(blocking=False):
            raise HTTPException(409, "a run is in progress")
        try:
            self.tb.discover(timeout=3)
        finally:
            self.busy.release()

    def start(self, s: Scenario) -> None:
        if not self.busy.acquire(blocking=False):
            raise HTTPException(409, "a run is already in progress")
        self._current = s
        self.last_error = ""

        def work():
            try:
                self.runner.run_campaign(s)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                self.runner.progress.phase = "error"
                self.runner.progress.error = str(e)
            finally:
                self.busy.release()

        self.thread = threading.Thread(target=work, daemon=True, name="campaign")
        self.thread.start()


def _scenario_or_422(data: Any) -> Scenario:
    try:
        return Scenario.model_validate(data)
    except ValidationError as e:
        raise HTTPException(422, [{"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]} for err in e.errors()])


def _safe(root: pathlib.Path, *parts: str) -> pathlib.Path:
    for p in parts:
        if not SAFE.match(p) or p in (".", ".."):
            raise HTTPException(400, "bad path")
    path = root.joinpath(*parts).resolve()
    if root.resolve() not in path.parents and path != root.resolve():
        raise HTTPException(400, "bad path")
    return path


def create_app(state: App) -> FastAPI:
    api = FastAPI(title="FaultBed", version=__version__)

    @api.get("/api/state")
    def get_state():
        p = state.runner.progress
        return {"version": __version__, "mode": state.mode, "boards": state.boards(), "transports": state.transports(),
                "transport_params": TRANSPORT_PARAMS, "wifi_transports": sorted(WIFI_TRANSPORTS),
                "progress": p.__dict__, "busy": state.busy.locked(), "error": state.last_error,
                "presets": list(topo.PRESETS)}

    @api.post("/api/boards/scan")
    def scan():
        state.scan()
        return state.boards()

    @api.post("/api/boards/identify")
    def identify(port: str = Body(..., embed=True)):
        if port not in state.tb.boards:
            raise HTTPException(404, "unknown board")
        state.tb.identify(port)
        return {"ok": True}

    @api.get("/api/scenarios")
    def list_scenarios():
        out = []
        for p in sorted(state.scenarios.glob("*.y*ml")) + sorted(state.scenarios.glob("*.json")):
            try:
                d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                out.append({"file": p.name, "name": d.get("name", p.stem), "description": d.get("description", "")})
            except Exception as e:  # noqa: BLE001
                out.append({"file": p.name, "name": p.stem, "description": f"unreadable: {e}"})
        return out

    @api.get("/api/scenarios/{file}")
    def get_scenario(file: str):
        p = _safe(state.scenarios, file)
        if not p.exists():
            raise HTTPException(404, "not found")
        return _scenario_or_422(yaml.safe_load(p.read_text(encoding="utf-8"))).model_dump(mode="json")

    @api.put("/api/scenarios/{file}")
    def put_scenario(file: str, data: dict = Body(...)):
        if not file.endswith((".yaml", ".yml")):
            file += ".yaml"
        s = _scenario_or_422(data)
        p = _safe(state.scenarios, file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dump_yaml(s), encoding="utf-8")
        return {"ok": True, "file": p.name}

    @api.post("/api/validate")
    def validate(data: dict = Body(...)):
        s = _scenario_or_422(data)
        ids = s.node_ids
        routes = s.routes()
        variants = s.expand()
        warnings = []
        online = [b for b in state.tb.boards.values() if b.online]
        if len(online) < len(ids):
            warnings.append(f"scenario uses {len(ids)} nodes but only {len(online)} boards are online")
        for v in {x.transport.name for x in variants}:
            if v in WIFI_TRANSPORTS and not s.transport.params.get("wifi", {}).get("ssid"):
                warnings.append(f"'{v}' needs Wi-Fi credentials (transport.params.wifi)")
        per_run = s.duration_s + s.drain_s + 6
        return {
            "ok": True, "warnings": warnings,
            "edges": sorted(list(e) for e in s.edges()),
            "layout": {str(k): v for k, v in topo.layout(ids, s.topology.preset).items()},
            "paths": {str(f.id): topo.path(routes, f.src, f.dst) if s.topology.enforce else [f.src, f.dst]
                      for f in s.flows},
            "variants": [v.name for v in variants], "runs": len(variants) * s.repeats,
            "estimated_s": len(variants) * s.repeats * per_run,
            "node_configs": {str(i): s.node_config(i, s.seed) for i in ids},
            "yaml": dump_yaml(s),
        }

    @api.post("/api/run")
    def run(data: dict = Body(...)):
        s = _scenario_or_422(data)
        state.start(s)
        return {"ok": True}

    @api.post("/api/stop")
    def stop():
        state.runner.stop_requested.set()
        return {"ok": True}

    @api.get("/api/results")
    def results():
        out = []
        if not state.results.exists():
            return out
        for d in sorted(state.results.iterdir(), reverse=True):
            if not d.is_dir() or not (d / "campaign.yaml").exists():
                continue
            camp = d / "campaign.json"
            agg = json.loads(camp.read_text(encoding="utf-8"))["aggregate"] if camp.exists() else []
            runs = sorted(str(p.parent.relative_to(d)).replace("\\", "/") for p in d.glob("v*/rep*/metrics.json"))
            name = (yaml.safe_load((d / "campaign.yaml").read_text(encoding="utf-8")) or {}).get("name", d.name)
            out.append({"id": d.name, "name": name, "runs": runs, "complete": camp.exists(), "aggregate": agg})
        return out

    @api.get("/api/results/{cid}/{variant}/{rep}")
    def run_metrics(cid: str, variant: str, rep: str):
        d = _safe(state.results, cid, variant, rep)
        f = d / "metrics.json"
        if not f.exists():
            raise HTTPException(404, "not found")
        m = json.loads(f.read_text(encoding="utf-8"))
        m["scenario"] = yaml.safe_load((d / "scenario.yaml").read_text(encoding="utf-8"))
        return m

    @api.get("/api/download/{cid}/{name}")
    def download(cid: str, name: str):
        if name not in ("campaign.csv", "campaign.json", "campaign.yaml"):
            raise HTTPException(404, "not found")
        p = _safe(state.results, cid, name)
        if not p.exists():
            raise HTTPException(404, "not found")
        return FileResponse(p, filename=f"{cid}_{name}")

    @api.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        since = 0
        try:
            while True:
                snap = state.live.snapshot(since)
                if snap["log"]:
                    since = snap["log"][-1]["k"]
                p = state.runner.progress
                await sock.send_text(json.dumps({"progress": p.__dict__, "busy": state.busy.locked(),
                                                 "boards": state.boards(), **snap}, default=str))
                await asyncio.sleep(0.5)
        except (WebSocketDisconnect, RuntimeError):
            return

    api.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return api


def serve(host="127.0.0.1", port=8000, sim=0, ports=None, baud=921600, results="results", scenarios="scenarios",
          sim_speed=1.0) -> None:
    import uvicorn

    from ..cli import make_testbed

    tb = make_testbed(sim, ports, baud, sim_speed=sim_speed)
    mode = f"simulation ({sim} virtual boards)" if sim else "hardware"
    app_state = App(tb, pathlib.Path(results), pathlib.Path(scenarios), mode)
    try:
        tb.discover(timeout=3)
    except RunError:
        pass
    print(f"FaultBed {__version__} [{mode}] - {len([b for b in tb.boards.values() if b.online])} boards online")
    print(f"open http://{host}:{port}")
    uvicorn.run(create_app(app_state), host=host, port=port, log_level="warning")
