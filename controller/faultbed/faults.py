"""Python mirror of the firmware FaultEngine (firmware/lib/fbcore/fault_engine.cpp).

Used for three things:
  * the simulator (so the whole system runs without hardware),
  * the post-run *fault audit*: replaying every logged arrival and checking the
    node made exactly the decision the model prescribes,
  * predicting expected loss before a run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from . import hashing as H

LOSS_NONE, LOSS_BERNOULLI, LOSS_GILBERT_ELLIOTT = 0, 1, 2
MODEL_IDS = {"none": LOSS_NONE, "bernoulli": LOSS_BERNOULLI, "gilbert_elliott": LOSS_GILBERT_ELLIOTT}
MAX_NODES = 32


@dataclass
class LinkRule:
    model: str = "none"
    p_loss: float = 0.0
    ge_p_gb: float = 0.0
    ge_p_bg: float = 1.0
    ge_loss_good: float = 0.0
    ge_loss_bad: float = 1.0
    p_corrupt: float = 0.0
    p_duplicate: float = 0.0
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    blackout: bool = False
    # exact integer microseconds when reconstructed from a node log (avoids ms->us float round trips)
    exact_delay_us: Optional[int] = None
    exact_jitter_us: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "LinkRule":
        if not d:
            return cls()
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @classmethod
    def from_node_log(cls, ev: dict) -> "LinkRule":
        """Rebuild the rule from a firmware `rule` event (integer model id, microsecond delays)."""
        names = {v: k for k, v in MODEL_IDS.items()}
        return cls(model=names.get(int(ev.get("model", 0)), "none"), p_loss=float(ev.get("p", 0)),
                   ge_p_gb=float(ev.get("gb", 0)), ge_p_bg=float(ev.get("bg", 1)), ge_loss_good=float(ev.get("lg", 0)),
                   ge_loss_bad=float(ev.get("lb", 1)), p_corrupt=float(ev.get("cor", 0)),
                   p_duplicate=float(ev.get("dup", 0)), delay_ms=int(ev.get("delay", 0)) / 1000.0,
                   jitter_ms=int(ev.get("jit", 0)) / 1000.0, blackout=bool(ev.get("bo", 0)),
                   exact_delay_us=int(ev.get("delay", 0)), exact_jitter_us=int(ev.get("jit", 0)))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__ if not k.startswith("exact_")}

    # integer microseconds exactly as the firmware computes them
    @property
    def delay_us(self) -> int:
        return self.exact_delay_us if self.exact_delay_us is not None else int(self.delay_ms * 1000.0)

    @property
    def jitter_us(self) -> int:
        return self.exact_jitter_us if self.exact_jitter_us is not None else int(self.jitter_ms * 1000.0)

    def expected_loss(self) -> float:
        if self.blackout:
            return 1.0
        if self.model == "bernoulli":
            return self.p_loss
        if self.model == "gilbert_elliott":
            denom = self.ge_p_gb + self.ge_p_bg
            pi_bad = self.ge_p_gb / denom if denom > 0 else 0.0
            return pi_bad * self.ge_loss_bad + (1 - pi_bad) * self.ge_loss_good
        return 0.0


@dataclass
class Verdict:
    index: int
    lost: bool = False
    corrupt: bool = False
    corrupt_bit: int = 0
    duplicate: bool = False
    delay_us: int = 0
    ge_bad: bool = False
    rule_version: int = 0

    @property
    def label(self) -> str:
        return "loss" if self.lost else "cor" if self.corrupt else "dup" if self.duplicate else "ok"


@dataclass
class _Compiled:
    rule: LinkRule
    t_loss: int
    t_gb: int
    t_bg: int
    t_lg: int
    t_lb: int
    t_cor: int
    t_dup: int

    @classmethod
    def of(cls, r: LinkRule) -> "_Compiled":
        t = H.prob_threshold
        return cls(r, t(r.p_loss), t(r.ge_p_gb), t(r.ge_p_bg), t(r.ge_loss_good), t(r.ge_loss_bad),
                   t(r.p_corrupt), t(r.p_duplicate))


@dataclass
class _LinkState:
    override: Optional[_Compiled] = None
    arrivals: int = 0
    ge_bad: bool = False


class FaultEngine:
    def __init__(self, seed: int, self_id: int):
        self.seed = seed & H.MASK64
        self.self_id = self_id
        self.version = 0
        self.default = _Compiled.of(LinkRule())
        self.links: Dict[int, _LinkState] = {}

    def _ls(self, src: int) -> _LinkState:
        return self.links.setdefault(src, _LinkState())

    def set_default_rule(self, r: LinkRule) -> None:
        self.default = _Compiled.of(r)
        for ls in self.links.values():
            if ls.override is None:
                ls.ge_bad = False
        self.version += 1

    def set_link_rule(self, src: int, r: LinkRule) -> None:
        if not 0 < src < MAX_NODES:
            return
        ls = self._ls(src)
        ls.override = _Compiled.of(r)
        ls.ge_bad = False
        self.version += 1

    def clear_link_rule(self, src: int) -> None:
        if not 0 < src < MAX_NODES:
            return
        ls = self._ls(src)
        ls.override = None
        ls.ge_bad = False
        self.version += 1

    def decide(self, src: int, frame_len: int) -> Verdict:
        v = Verdict(index=0, rule_version=self.version)
        if not 0 < src < MAX_NODES:
            v.lost = True
            return v
        ls = self._ls(src)
        c = ls.override or self.default
        idx = ls.arrivals
        ls.arrivals += 1
        v.index = idx
        s, me, d = self.seed, self.self_id, H.draw

        threshold = 0
        if c.rule.model == "bernoulli":
            threshold = c.t_loss
        elif c.rule.model == "gilbert_elliott":
            r = d(s, src, me, H.STREAM_GE, idx)
            if ls.ge_bad:
                if H.bernoulli(r, c.t_bg):
                    ls.ge_bad = False
            elif H.bernoulli(r, c.t_gb):
                ls.ge_bad = True
            threshold = c.t_lb if ls.ge_bad else c.t_lg
        v.ge_bad = ls.ge_bad

        if c.rule.blackout or H.bernoulli(d(s, src, me, H.STREAM_LOSS, idx), threshold):
            v.lost = True
            return v
        if frame_len > 0 and H.bernoulli(d(s, src, me, H.STREAM_CORRUPT, idx), c.t_cor):
            v.corrupt = True
            v.corrupt_bit = d(s, src, me, H.STREAM_CORRUPT_BIT, idx) % (frame_len * 8)
        v.duplicate = H.bernoulli(d(s, src, me, H.STREAM_DUP, idx), c.t_dup)
        v.delay_us = c.rule.delay_us
        if c.rule.jitter_us:
            v.delay_us += d(s, src, me, H.STREAM_JITTER, idx) % (c.rule.jitter_us + 1)
        return v
