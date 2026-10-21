"""Discrete-event simulator of FaultBed boards.

Simulated boards speak exactly the same serial JSON protocol as the firmware
and run a line-by-line port of the node core (routing, fault engine, echo,
logging). On top of that, a crude per-protocol channel model adds natural
latency/loss and protocol behaviour (Wi-Fi association time, MQTT broker hop,
CoAP CON/ACK retransmission).

Purpose: develop and test the controller, metrics and web UI without
hardware, and give CI something deterministic to run. It is *not* a substitute
for the hardware results - that's the whole point of the testbed.
"""
from __future__ import annotations

import heapq
import itertools
import math
import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from . import hashing as H
from . import packet as P
from .faults import FaultEngine, LinkRule
from .ports import Clock, EventCallback, NodePort

FW_VERSION = "0.1.0-sim"


@dataclass
class ChannelProfile:
    latency_ms: float      # one-way, per hop
    jitter_ms: float
    natural_loss: float    # per frame, after link-layer retries
    connect_s: tuple       # (min, max) time to bring the link up
    via_broker: bool = False
    coap: bool = False


PROFILES: Dict[str, ChannelProfile] = {
    "espnow": ChannelProfile(1.2, 0.6, 0.005, (0.0, 0.0)),
    "udp": ChannelProfile(3.5, 3.0, 0.01, (1.5, 3.0)),
    "mqtt": ChannelProfile(4.5, 3.0, 0.0, (2.0, 4.0), via_broker=True),  # each leg; two legs per hop
    "coap": ChannelProfile(3.5, 3.0, 0.01, (1.5, 3.0), coap=True),
}
GENERIC = ChannelProfile(5.0, 2.0, 0.01, (1.0, 2.0))


class SimWorld(Clock):
    def __init__(self, seed: int = 0, serial_latency_ms: float = 0.3, realtime: float = 0.0):
        """realtime=0: run as fast as possible (tests, batch). realtime=1.0: pace virtual time to the wall clock
        (web UI demos, so the live view behaves like real hardware). 2.0 = twice as fast, etc."""
        self.realtime = realtime
        self.t_ns = 0
        self._q: list = []
        self._ctr = itertools.count()
        self.rng = random.Random(seed)
        self.serial_ns = int(serial_latency_ms * 1e6)
        self.boards: List["SimBoard"] = []

    # ---- Clock ----
    def now_ns(self) -> int:
        return self.t_ns

    def sleep(self, seconds: float) -> None:
        if self.realtime > 0:
            import time

            time.sleep(seconds / self.realtime)
        self.run_until(self.t_ns + max(1, int(seconds * 1e9)))  # always advance, or callers spin forever

    # ---- event loop ----
    def at(self, t_ns: int, fn: Callable[[], None]) -> None:
        heapq.heappush(self._q, (max(t_ns, self.t_ns), next(self._ctr), fn))

    def after(self, delay_ns: int, fn: Callable[[], None]) -> None:
        self.at(self.t_ns + max(0, int(delay_ns)), fn)

    def run_until(self, t_ns: int) -> None:
        while self._q and self._q[0][0] <= t_ns:
            t, _, fn = heapq.heappop(self._q)
            self.t_ns = t
            fn()
        self.t_ns = max(self.t_ns, t_ns)

    def add_boards(self, n: int) -> List["SimNodePort"]:
        ports = []
        for _ in range(n):
            k = len(self.boards) + 1
            b = SimBoard(self, k)
            self.boards.append(b)
            ports.append(SimNodePort(b))
        return ports

    def board(self, node_id: int) -> Optional["SimBoard"]:
        for b in self.boards:
            if b.id == node_id:
                return b
        return None


class SimNodePort(NodePort):
    def __init__(self, board: "SimBoard"):
        self.board = board
        self.name = f"SIM{board.index}"
        self._cb: Optional[EventCallback] = None
        board.port = self

    def open(self, on_event: EventCallback) -> None:
        self._cb = on_event
        self.board.world.after(self.board.world.serial_ns, self.board.hello)

    def send(self, cmd: dict) -> None:
        w = self.board.world
        w.after(w.serial_ns, lambda c=dict(cmd): self.board.handle(c))

    def close(self) -> None:
        self._cb = None

    def deliver(self, ev: dict) -> None:
        w = self.board.world
        w.after(w.serial_ns, lambda e=ev: self._cb and self._cb(self, e, w.t_ns))


class SimBoard:
    def __init__(self, world: SimWorld, index: int):
        self.world = world
        self.index = index
        self.mac = f"5A:1E:00:00:00:{index:02X}"
        self.port: Optional[SimNodePort] = None
        r = world.rng
        self.offset_us = r.randint(1_000_000, 50_000_000)  # boards booted at different times
        self.drift = r.uniform(-30e-6, 30e-6)              # crystal error, ~ +-30 ppm
        self.id = 0
        self._reset_state()

    # ---- helpers ----
    def now_us(self) -> int:
        return int(self.world.t_ns / 1000 * (1 + self.drift)) + self.offset_us

    def emit(self, ev: dict) -> None:
        if self.port:
            self.port.deliver(ev)

    def _reset_state(self) -> None:
        self.cfg: dict = {}
        self.engine = FaultEngine(1, max(self.id, 1))
        self.running = False
        self.down = False
        self.hard_killed = False
        self.flows: List[dict] = []
        self.seq: Dict[int, int] = {}
        self.gen = 0
        self.tr_name = ""
        self.profile = GENERIC
        self.link_up = False
        self.link_gen = 0
        self.neighbors: set = set()
        self.routes: Dict[int, int] = {}
        self.ttl = 8
        self.neighbor_filter = True
        self.coap_pending: Dict[int, dict] = {}
        self.coap_seen: List[tuple] = []
        self.coap_mid = 0
        self.stats = dict(gen=0, sendfail=0, rx=0, fwd=0, echo=0, arr=0, loss=0, cor=0, dup=0, crc=0, ttl=0,
                          noroute=0, queue=0, down=0, malformed=0, overheard=0, filtered=0)
        self.tcount = dict(ok=0, err=0, ups=0, downs=0, retx=0, giveup=0, rxovf=0)

    def ack(self, c: str, ok: bool = True, err: Optional[str] = None) -> None:
        ev = {"e": "ack", "c": c, "ok": int(ok)}
        if err:
            ev["err"] = err
        self.emit(ev)

    def hello(self) -> None:
        self.emit({"e": "hello", "fw": FW_VERSION, "mac": self.mac, "chip": "SIM", "id": self.id, "heap": 200000,
                   "transports": list(PROFILES)})

    # ---- control plane (mirror of src/main.cpp) ----
    def handle(self, c: dict) -> None:
        cmd = c.get("c", "")
        if cmd == "hello":
            self.hello()
        elif cmd == "config":
            self._config(c)
        elif cmd == "peers":
            self.ack("peers")
        elif cmd == "start":
            self._start()
            self.ack("start")
        elif cmd == "stop":
            self.running = False
            self.gen += 1
            self.emit({"e": "stop", "t": self.now_us()})
            self._log_stats()
            self.ack("stop")
        elif cmd == "kill":
            self._set_down(True)
            if c.get("hard"):
                self._transport_end()
                self.hard_killed = True
            self.ack("kill")
        elif cmd == "revive":
            if self.hard_killed:
                self._transport_begin()
                self.hard_killed = False
            self._set_down(False)
            self.ack("revive")
        elif cmd == "fault":
            self._apply_rule(int(c.get("src", -1)), c.get("rule"))
            self.ack("fault")
        elif cmd == "sync":
            self.emit({"e": "sync", "h": c.get("h", 0), "t": self.now_us()})
        elif cmd == "stats":
            self._log_stats()
        elif cmd == "led":
            self.ack("led")
        elif cmd == "reboot":
            self.ack("reboot")
            self._transport_end()
            self.id = 0
            self._reset_state()
            self.world.after(300_000_000, self.hello)
        else:
            self.ack(cmd, False, "unknown command")

    def _config(self, c: dict) -> None:
        nid = int(c.get("id", 0))
        name = c.get("transport", {}).get("name", "")
        if not 0 < nid < 32:
            return self.ack("config", False, "bad id")
        if name not in PROFILES:
            return self.ack("config", False, "unknown transport")
        self._transport_end()
        self.id = nid
        self._reset_state()
        self.cfg = c
        self.engine = FaultEngine(int(c.get("seed", "1")), nid)
        self.ttl = int(c.get("ttl", 8))
        self.neighbor_filter = bool(c.get("neighbor_filter", True))
        self.neighbors = set(int(x) for x in c.get("neighbors", []))
        self.routes = {int(k): int(v) for k, v in c.get("routes", {}).items()}
        self.flows = list(c.get("flows", []))
        f = c.get("faults", {})
        self._apply_rule(-1, f.get("default"))
        for k, v in f.get("links", {}).items():
            self._apply_rule(int(k), v)
        self.tr_name = name
        self.tr_cfg = c.get("transport", {})
        self.profile = PROFILES[name]
        self._transport_begin()
        self.ack("config")

    def _apply_rule(self, src: int, rule: Optional[dict]) -> None:
        r = LinkRule.from_dict(rule)
        if rule is None:
            if src < 0:
                self.engine.set_default_rule(LinkRule())
            else:
                self.engine.clear_link_rule(src)
        elif src < 0:
            self.engine.set_default_rule(r)
        else:
            self.engine.set_link_rule(src, r)
        self.emit({"e": "rule", "t": self.now_us(), "rv": self.engine.version, "src": src,
                   **_rule_log_fields(r), "clr": int(rule is None)})

    def _log_stats(self) -> None:
        self.emit({"e": "stats", "t": self.now_us(), **self.stats})
        self.emit({"e": "tstats", "t": self.now_us(), "tr": self.tr_name, **self.tcount})

    # ---- transport model ----
    def _transport_begin(self) -> None:
        self.link_gen += 1
        g = self.link_gen
        lo, hi = self.profile.connect_s
        delay = self.world.rng.uniform(lo, hi) if hi > 0 else 0.0

        def up() -> None:
            if g != self.link_gen:
                return
            self.link_up = True
            self.tcount["ups"] += 1
            ev = {"e": "link", "t": self.now_us(), "tr": self.tr_name, "up": 1}
            if self.tr_name != "espnow":
                ev.update(ip=f"10.0.0.{self.id + 100}", ch=6, wrssi=-48)
            self.emit(ev)

        self.world.after(int(delay * 1e9), up)

    def _transport_end(self) -> None:
        self.link_gen += 1
        if self.link_up:
            self.link_up = False
            self.tcount["downs"] += 1
            self.emit({"e": "link", "t": self.now_us(), "tr": self.tr_name, "up": 0})
        self.coap_pending.clear()

    def _hop_latency_ns(self) -> int:
        p = self.profile
        legs = 2 if p.via_broker else 1
        ms = sum(p.latency_ms + self.world.rng.uniform(0, p.jitter_ms) for _ in range(legs))
        return int(ms * 1e6)

    def _air(self, dst: int, frame: bytes) -> bool:
        """Put a frame on the simulated channel towards node dst."""
        if not self.link_up:
            return False
        # unicast reaches only its addressee (MAC filter / IP / MQTT topic); broadcast reaches everyone
        targets = [b for b in self.world.boards if b is not self and b.tr_name == self.tr_name and
                   (dst == P.BROADCAST or b.id == dst)]
        for b in targets:
            if self.world.rng.random() < self.profile.natural_loss:
                continue
            self.world.after(self._hop_latency_ns(), lambda b=b, f=frame: b._on_air(f))
        self.tcount["ok"] += 1
        return True

    def _on_air(self, frame: bytes) -> None:
        if not self.link_up:
            return
        if self.profile.coap:
            return self._coap_receive(frame)
        self.admit(frame, -45)

    def transmit(self, hop_dst: int, frame: bytes) -> bool:
        if not self.link_up:
            return False
        if self.profile.coap:
            return self._coap_send(hop_dst, frame)
        return self._air(hop_dst, frame)

    # ---- CoAP CON/ACK model (mirrors coap_transport.cpp) ----
    def _coap_send(self, hop_dst: int, frame: bytes) -> bool:
        confirmable = self.tr_cfg.get("confirmable", True)
        if not confirmable:
            return self._air(hop_dst, frame)
        if len(self.coap_pending) >= 8:
            self.tcount["err"] += 1
            return False
        mid = ((self.id << 8) + self.coap_mid) & 0xFFFF
        self.coap_mid += 1
        ack_to = float(self.tr_cfg.get("ack_timeout_ms", 2000)) * 1e6
        timeout = int(ack_to + ack_to * (H.splitmix64(mid) % 500) / 1000)
        self.coap_pending[mid] = dict(dst=hop_dst, frame=frame, attempts=0, timeout=timeout)
        self._coap_tx(mid)
        return True

    def _coap_tx(self, mid: int) -> None:
        p = self.coap_pending.get(mid)
        if not p:
            return
        gen = self.link_gen
        dst = self.world.board(p["dst"])
        if dst and dst.tr_name == self.tr_name and self.world.rng.random() >= self.profile.natural_loss:
            self.world.after(self._hop_latency_ns(), lambda: dst._coap_receive(p["frame"], mid=mid, sender=self))
        self.tcount["ok"] += 1

        def expire() -> None:
            q = self.coap_pending.get(mid)
            if q is not p or gen != self.link_gen:
                return
            if q["attempts"] >= int(self.tr_cfg.get("max_retransmit", 4)):
                del self.coap_pending[mid]
                self.tcount["giveup"] += 1
                self.emit({"e": "txerr", "t": self.now_us(), "tr": "coap", "why": "giveup", "mid": mid, "to": q["dst"]})
                return
            q["attempts"] += 1
            q["timeout"] *= 2
            self.tcount["retx"] += 1
            self._coap_tx(mid)

        self.world.after(p["timeout"], expire)

    def _coap_receive(self, frame: bytes, mid: Optional[int] = None, sender: Optional["SimBoard"] = None) -> None:
        if not self.link_up:
            return
        if mid is None:  # NON
            self.admit(frame, -45)
            return
        src = frame[6] if len(frame) > 6 else 0
        if (src, mid) in self.coap_seen:
            if self.admit_control(src):
                self._coap_send_ack(sender, mid)
            return
        if self.admit(frame, -45):
            self.coap_seen = (self.coap_seen + [(src, mid)])[-128:]  # SEEN_SLOTS in coap_transport.cpp
            self._coap_send_ack(sender, mid)

    def _coap_send_ack(self, sender: Optional["SimBoard"], mid: int) -> None:
        if sender is None or self.world.rng.random() < self.profile.natural_loss:
            return
        me = self.id
        self.world.after(self._hop_latency_ns(), lambda: sender._coap_ack(me, mid))

    def _coap_ack(self, src: int, mid: int) -> None:
        if not self.link_up or not self.admit_control(src):
            return
        self.coap_pending.pop(mid, None)

    # ---- node core (mirror of lib/fbcore/node.cpp) ----
    def _set_down(self, d: bool) -> None:
        self.down = d
        self.emit({"e": "down" if d else "up", "t": self.now_us()})

    def _start(self) -> None:
        self.gen += 1
        g = self.gen
        self.running = True
        self.emit({"e": "start", "t": self.now_us()})
        for f in self.flows:
            self.seq[f["id"]] = 0
            self.world.after(int(f.get("start_ms", 0)) * 1_000_000, lambda f=f: self._flow_fire(f, g))

    def _interval_ns(self, f: dict, seq: int) -> int:
        mean_us = int(f.get("interval_ms", 1000)) * 1000
        if f.get("pattern") != "poisson":
            return mean_us * 1000
        r = H.draw(self.engine.seed, self.id, int(f["id"]), H.STREAM_INTERVAL, seq)
        u = (r >> 11) / 9007199254740992.0
        x = min(-math.log(1.0 - u) * mean_us, 20.0 * mean_us)
        return int(x) * 1000

    def _flow_fire(self, f: dict, g: int) -> None:
        if g != self.gen or not self.running:
            return
        count = int(f.get("count", 0))
        if not self.down:
            self._send_data(f)
        if count and self.seq[f["id"]] >= count:
            return
        self.world.after(self._interval_ns(f, self.seq[f["id"]]), lambda: self._flow_fire(f, g))

    def _next_hop(self, dst: int) -> int:
        return self.routes.get(dst, dst)

    def _send_data(self, f: dict) -> None:
        fid, dst = int(f["id"]), int(f["dst"])
        seq = self.seq[fid]
        self.seq[fid] = seq + 1
        now = self.now_us()
        h = P.Header(type=P.FT_DATA, flags=P.FF_ECHO_REQUEST if f.get("echo", True) else 0, origin=self.id,
                     final_dst=dst, hop_src=self.id, hop_dst=self._next_hop(dst), ttl=self.ttl, flow=fid, seq=seq,
                     origin_us=now & 0xFFFFFFFF, payload_len=min(int(f.get("size", 32)), P.MAX_PAYLOAD))
        frame = P.encode(h)
        ok = self.transmit(h.hop_dst, frame)
        self.stats["gen"] += 1
        if not ok:
            self.stats["sendfail"] += 1
        self.emit({"e": "tx", "t": now, "f": fid, "s": seq, "d": dst, "n": h.hop_dst, "l": len(frame), "ok": int(ok)})

    def admit(self, frame: bytes, rssi: int) -> bool:
        ok, h = P.decode(frame)
        if not ok:
            self.stats["malformed"] += 1
            return False
        if h.hop_dst not in (self.id, P.BROADCAST):
            self.stats["overheard"] += 1
            return False
        if not 0 < h.hop_src < 32 or (self.neighbor_filter and h.hop_src not in self.neighbors):
            self.stats["filtered"] += 1
            return False
        if self.down:
            self.stats["down"] += 1
            return False
        v = self.engine.decide(h.hop_src, len(frame))
        self.stats["arr"] += 1
        self.emit({"e": "arr", "t": self.now_us(), "src": h.hop_src, "i": v.index, "rv": v.rule_version, "v": v.label,
                   "dl": v.delay_us, "o": h.origin, "f": h.flow, "s": h.seq, "ty": h.type, "rssi": rssi})
        if v.lost:
            self.stats["loss"] += 1
            return False
        delay_ns = v.delay_us * 1000
        if v.corrupt:
            self.stats["cor"] += 1
            bad = P.flip_bit(frame, v.corrupt_bit)
            self.world.after(delay_ns, lambda: self._process(bad))
            return False
        self.world.after(delay_ns, lambda: self._process(frame))
        if v.duplicate:
            self.stats["dup"] += 1
            self.world.after(delay_ns, lambda: self._process(frame))
        return True

    def admit_control(self, src: int) -> bool:
        if self.down or not 0 < src < 32 or (self.neighbor_filter and src not in self.neighbors):
            return False
        v = self.engine.decide(src, 0)
        self.emit({"e": "arr", "t": self.now_us(), "src": src, "i": v.index, "rv": v.rule_version, "v": v.label,
                   "ctl": 1})
        if v.lost:
            self.stats["loss"] += 1
        return not v.lost

    def _drop(self, why: str, h: P.Header) -> None:
        self.emit({"e": "drop", "t": self.now_us(), "why": why, "o": h.origin, "f": h.flow, "s": h.seq, "ty": h.type})

    def _process(self, frame: bytes) -> None:
        ok, h = P.decode(frame)
        if not ok:
            self.stats["crc"] += 1
            self.emit({"e": "drop", "t": self.now_us(), "why": "crc"})
            return
        if self.down:
            self.stats["down"] += 1
            return
        now = self.now_us()
        if h.final_dst == self.id:
            if h.type == P.FT_ECHO_REPLY:
                self.stats["echo"] += 1
                rtt = (now - h.origin_us) & 0xFFFFFFFF
                self.emit({"e": "rtt", "t": now, "f": h.flow, "s": h.seq, "rtt": rtt, "hops": h.hops + 1})
                return
            self.stats["rx"] += 1
            self.emit({"e": "rx", "t": now, "o": h.origin, "f": h.flow, "s": h.seq, "l": h.payload_len,
                       "hops": h.hops + 1})
            if h.flags & P.FF_ECHO_REQUEST:
                r = P.Header(type=P.FT_ECHO_REPLY, flags=0, origin=self.id, final_dst=h.origin, hop_src=self.id,
                             hop_dst=self._next_hop(h.origin), ttl=self.ttl, hops=0, flow=h.flow, seq=h.seq,
                             origin_us=h.origin_us, payload_len=h.payload_len)
                sent = self.transmit(r.hop_dst, P.encode(r, frame[P.HEADER_LEN:]))
                if not sent:
                    self.stats["sendfail"] += 1
                self.emit({"e": "etx", "t": self.now_us(), "o": h.origin, "f": h.flow, "s": h.seq, "n": r.hop_dst,
                           "ok": int(sent)})
            return
        if h.ttl <= 1:
            self.stats["ttl"] += 1
            return self._drop("ttl", h)
        nh = self._next_hop(h.final_dst)
        if nh in (0, self.id) or nh >= 32:
            self.stats["noroute"] += 1
            return self._drop("noroute", h)
        sent = self.transmit(nh, P.rewrite_hop(frame, self.id, nh, h.ttl - 1, h.hops + 1))
        if sent:
            self.stats["fwd"] += 1
        else:
            self.stats["sendfail"] += 1
        self.emit({"e": "fwd", "t": now, "o": h.origin, "f": h.flow, "s": h.seq, "ty": h.type, "n": nh, "ok": int(sent)})


def _rule_log_fields(r: LinkRule) -> dict:
    model = {"none": 0, "bernoulli": 1, "gilbert_elliott": 2}[r.model]
    return dict(model=model, p=r.p_loss, gb=r.ge_p_gb, bg=r.ge_p_bg, lg=r.ge_loss_good, lb=r.ge_loss_bad,
                cor=r.p_corrupt, dup=r.p_duplicate, delay=r.delay_us, jit=r.jitter_us, bo=int(r.blackout))
