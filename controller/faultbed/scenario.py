"""Scenario = the complete, versionable description of one experiment.

A scenario file (YAML/JSON) plus the firmware version is everything needed to
re-run an experiment on another set of boards. Every run directory stores the
exact resolved scenario it was produced from.
"""
from __future__ import annotations

import copy
import itertools
import json
import pathlib
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from . import topology as topo
from .faults import LinkRule

TRANSPORTS = ("espnow", "udp", "mqtt", "coap")


class RuleModel(BaseModel):
    model: Literal["none", "bernoulli", "gilbert_elliott"] = "none"
    p_loss: float = Field(0.0, ge=0, le=1)
    ge_p_gb: float = Field(0.0, ge=0, le=1)
    ge_p_bg: float = Field(1.0, ge=0, le=1)
    ge_loss_good: float = Field(0.0, ge=0, le=1)
    ge_loss_bad: float = Field(1.0, ge=0, le=1)
    p_corrupt: float = Field(0.0, ge=0, le=1)
    p_duplicate: float = Field(0.0, ge=0, le=1)
    delay_ms: float = Field(0.0, ge=0, le=10000)
    jitter_ms: float = Field(0.0, ge=0, le=10000)
    blackout: bool = False

    def to_rule(self) -> LinkRule:
        return LinkRule(**self.model_dump())


class FaultsModel(BaseModel):
    default: RuleModel = RuleModel()
    # "a->b": applied to frames from a as received by b (directed)
    links: Dict[str, RuleModel] = {}

    @field_validator("links")
    @classmethod
    def _keys(cls, v: Dict[str, RuleModel]) -> Dict[str, RuleModel]:
        for k in v:
            parse_link(k)
        return v


def parse_link(key: str) -> tuple[int, int]:
    try:
        a, b = key.replace(" ", "").split("->")
        return int(a), int(b)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"link key must look like '1->2', got {key!r}") from e


class NodeSpec(BaseModel):
    id: int = Field(ge=1, le=31)
    port: Optional[str] = None   # serial port (COM5, /dev/ttyUSB0); None = assign automatically
    mac: Optional[str] = None    # pin a scenario id to a physical board
    label: Optional[str] = None


class TopologyModel(BaseModel):
    preset: Literal["full", "line", "ring", "star", "tree", "grid", "custom"] = "full"
    center: Optional[int] = None
    edges: List[List[int]] = []
    enforce: bool = True  # False: every node hears everyone (physical broadcast domain)


class FlowModel(BaseModel):
    id: int = Field(ge=1, le=255)
    src: int
    dst: int
    interval_ms: int = Field(200, ge=5)
    size: int = Field(32, ge=0, le=226)
    echo: bool = True
    pattern: Literal["periodic", "poisson"] = "periodic"
    count: int = Field(0, ge=0)
    start_ms: int = Field(0, ge=0)


class TimelineEvent(BaseModel):
    at_s: float = Field(ge=0)
    action: Literal["kill", "revive", "fault", "clear_fault"]
    node: Optional[int] = None       # kill / revive
    hard: bool = False               # kill: also power down the radio stack
    link: Optional[str] = None       # fault: "a->b" or "default"
    rule: Optional[RuleModel] = None


class TransportModel(BaseModel):
    name: str = "espnow"
    params: Dict[str, Any] = {}

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        # Not restricted to TRANSPORTS on purpose: new firmware transports work without controller changes.
        if not v or not v.replace("_", "").isalnum():
            raise ValueError("transport name must be alphanumeric")
        return v


class Scenario(BaseModel):
    name: str = "unnamed"
    description: str = ""
    seed: int = Field(1, ge=0, lt=2 ** 64)
    nodes: List[NodeSpec]
    transport: TransportModel = TransportModel()
    topology: TopologyModel = TopologyModel()
    ttl: int = Field(8, ge=1, le=32)
    faults: FaultsModel = FaultsModel()
    flows: List[FlowModel] = []
    timeline: List[TimelineEvent] = []
    duration_s: float = Field(30, gt=0)
    warmup_s: float = Field(2, ge=0)      # excluded from metrics
    drain_s: float = Field(3, ge=0)       # wait for in-flight packets after traffic stops
    repeats: int = Field(1, ge=1, le=100)
    vary_seed: bool = False               # True: seed + repeat index (False keeps injected faults identical)
    sweep: Dict[str, List[Any]] = {}      # dotted path -> values, e.g. {"transport.name": ["espnow","udp"]}

    @model_validator(mode="after")
    def _check(self) -> "Scenario":
        ids = self.node_ids
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate node ids")
        for f in self.flows:
            if f.src not in ids or f.dst not in ids or f.src == f.dst:
                raise ValueError(f"flow {f.id}: src/dst must be distinct scenario nodes")
        if len({f.id for f in self.flows}) != len(self.flows):
            raise ValueError("duplicate flow ids")
        for k in self.faults.links:
            a, b = parse_link(k)
            if a not in ids or b not in ids:
                raise ValueError(f"fault link {k}: unknown node")
        for ev in self.timeline:
            if ev.action in ("kill", "revive") and ev.node not in ids:
                raise ValueError(f"timeline {ev.action}@{ev.at_s}: unknown node {ev.node}")
            if ev.action in ("fault", "clear_fault"):
                if not ev.link:
                    raise ValueError(f"timeline {ev.action}@{ev.at_s}: 'link' required")
                if ev.link != "default":
                    parse_link(ev.link)
            if ev.action == "fault" and ev.rule is None:
                raise ValueError(f"timeline fault@{ev.at_s}: 'rule' required")
        if self.topology.enforce and not topo.is_connected(self.edges(), ids):
            raise ValueError("topology is not connected")
        return self

    # ---- derived ----
    @property
    def node_ids(self) -> List[int]:
        return [n.id for n in self.nodes]

    def edges(self) -> set:
        t = self.topology
        return topo.build_edges(t.preset, self.node_ids, t.center, t.edges)

    def routes(self) -> Dict[int, Dict[int, int]]:
        ids = self.node_ids
        if not self.topology.enforce:
            return {i: {} for i in ids}
        return topo.shortest_path_routes(self.edges(), ids)

    def node_config(self, node_id: int, seed: int) -> dict:
        """The exact JSON the firmware receives in its `config` command."""
        ids = self.node_ids
        nb = topo.neighbors(self.edges(), ids)[node_id] if self.topology.enforce else [i for i in ids if i != node_id]
        links = {}
        for k, rule in self.faults.links.items():
            a, b = parse_link(k)
            if b == node_id:
                links[str(a)] = rule.model_dump()
        return {
            "c": "config",
            "id": node_id,
            "seed": str(seed),
            "ttl": self.ttl,
            "neighbor_filter": self.topology.enforce,
            "neighbors": nb,
            "routes": {str(d): h for d, h in self.routes()[node_id].items()},
            "flows": [f.model_dump(exclude={"src"}) for f in self.flows if f.src == node_id],
            "faults": {"default": self.faults.default.model_dump(), "links": links},
            "transport": {"name": self.transport.name, **self.transport.params},
        }

    def expand(self) -> List["Scenario"]:
        """Cartesian product of the sweep; each variant has sweep cleared and a descriptive name."""
        if not self.sweep:
            return [self]
        keys = list(self.sweep)
        out = []
        for combo in itertools.product(*(self.sweep[k] for k in keys)):
            d = self.model_dump()
            d["sweep"] = {}
            tag = []
            for k, v in zip(keys, combo):
                set_path(d, k, v)
                tag.append(f"{k.split('.')[-1]}={v}")
            d["name"] = f"{self.name}[{','.join(tag)}]"
            out.append(Scenario.model_validate(d))
        return out


def set_path(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        if isinstance(cur, list):
            cur = cur[int(p)]
        else:
            cur = cur.setdefault(p, {})
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = copy.deepcopy(value)
    else:
        cur[last] = copy.deepcopy(value)


def load(path: str | pathlib.Path) -> Scenario:
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    data = json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)
    return Scenario.model_validate(data)


def dump_yaml(s: Scenario) -> str:
    return yaml.safe_dump(s.model_dump(mode="json"), sort_keys=False)
