"""Turn a run's raw event log into metrics.

Beyond the usual PDR / latency / throughput / recovery numbers, two analyses
are specific to this testbed:

* Loss attribution - every lost packet is traced along its path and blamed on
  injected loss, injected corruption, a failed node, a transport send failure,
  or the real channel (anything we did *not* inject). This separates the
  testbed's controlled faults from the radio's own behaviour.
* Fault audit - every arrival a node logged is replayed through the Python
  copy of the fault engine. Conformance must be 100%: it proves the injected
  faults were exactly the configured ones, which is the basis of the
  reproducibility claim.
"""
from __future__ import annotations

import csv
import json
import math
import pathlib
import statistics as st
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

from . import topology as topo
from .faults import FaultEngine, LinkRule
from .scenario import Scenario, parse_link


# ---------------- small stats helpers ----------------
def percentile(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def describe(xs: List[float]) -> dict:
    if not xs:
        return {"n": 0}
    jit = st.mean(abs(a - b) for a, b in zip(xs[1:], xs[:-1])) if len(xs) > 1 else 0.0
    return {"n": len(xs), "mean": st.mean(xs), "std": st.pstdev(xs), "min": min(xs), "p50": percentile(xs, 0.5),
            "p95": percentile(xs, 0.95), "p99": percentile(xs, 0.99), "max": max(xs), "jitter": jit}


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


# ---------------- clock mapping ----------------
class ClockMap:
    """node microseconds -> host nanoseconds, fitted from serial sync exchanges (min-RTT filtered)."""

    def __init__(self, samples: Iterable[Iterable[int]]):
        s = [tuple(int(v) for v in x) for x in samples]  # (host_send_ns, host_recv_ns, node_us)
        self.a, self.b, self.err_us, self.points = 0.0, 1.0, None, 0
        if not s:
            self.valid = False
            return
        self.valid = True
        s.sort(key=lambda x: x[0])
        windows, cur = [], [s[0]]
        for x in s[1:]:
            if x[0] - cur[-1][0] > 1e9:
                windows.append(cur)
                cur = []
            cur.append(x)
        windows.append(cur)
        pts = []
        for w in windows:
            best = sorted(w, key=lambda x: x[1] - x[0])[: max(1, len(w) // 4)]
            for snd, rcv, node in best:
                pts.append(((snd + rcv) / 2000.0, float(node)))  # (host_us, node_us)
        self.points = len(pts)
        self.err_us = min((x[1] - x[0]) for x in s) / 2000.0
        hx = [p[0] for p in pts]
        nx = [p[1] for p in pts]
        if len(windows) > 1 and max(hx) - min(hx) > 1e6:
            mh, mn = st.mean(hx), st.mean(nx)
            var = sum((h - mh) ** 2 for h in hx)
            self.b = sum((h - mh) * (n - mn) for h, n in zip(hx, nx)) / var
            self.a = mn - self.b * mh
        else:
            self.b = 1.0
            self.a = st.median(n - h for h, n in pts)

    def to_host_ns(self, node_us: float) -> float:
        return (node_us - self.a) / self.b * 1000.0

    def drift_ppm(self) -> float:
        return (self.b - 1.0) * 1e6


# ---------------- loading ----------------
def load_run(run_dir: pathlib.Path) -> Tuple[Scenario, dict, List[dict]]:
    s = Scenario.model_validate(yaml.safe_load((run_dir / "scenario.yaml").read_text(encoding="utf-8")))
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    events = []
    with open(run_dir / "events.jsonl", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    return s, meta, events


# ---------------- fault audit ----------------
def audit(seed: int, node_events: Dict[int, List[dict]]) -> dict:
    links: Dict[str, dict] = {}
    total = match = 0
    mismatches: List[dict] = []
    for nid, evs in node_events.items():
        eng = FaultEngine(seed, nid)
        rules_now: Dict[int, LinkRule] = {}
        default = LinkRule()
        for ev in evs:
            e = ev.get("e")
            if e == "rule":
                src = int(ev["src"])
                r = LinkRule.from_node_log(ev)
                if src < 0:
                    default = LinkRule() if ev.get("clr") else r
                    eng.set_default_rule(default)
                elif ev.get("clr"):
                    rules_now.pop(src, None)
                    eng.clear_link_rule(src)
                else:
                    rules_now[src] = r
                    eng.set_link_rule(src, r)
            elif e == "arr":
                src = int(ev["src"])
                ctl = bool(ev.get("ctl"))
                v = eng.decide(src, 0 if ctl else 1)
                rule = rules_now.get(src, default)
                ok = (v.index == int(ev["i"]) and v.label == ev["v"] and v.rule_version == int(ev.get("rv", v.rule_version))
                      and (v.lost or ctl or v.delay_us == int(ev.get("dl", v.delay_us))))
                total += 1
                match += ok
                if not ok and len(mismatches) < 20:
                    mismatches.append({"node": nid, "src": src, "logged": ev, "expected": v.label, "expected_index": v.index})
                key = f"{src}->{nid}"
                L = links.setdefault(key, {"link": key, "arrivals": 0, "injected_loss": 0, "injected_corrupt": 0,
                                           "injected_dup": 0, "expected_loss_sum": 0.0, "delays_ms": [], "rho_sum": 0.0})
                L["arrivals"] += 1
                L["expected_loss_sum"] += rule.expected_loss()
                if rule.model == "gilbert_elliott" and not rule.blackout:
                    L["rho_sum"] += max(0.0, 1.0 - rule.ge_p_gb - rule.ge_p_bg)
                if ev["v"] == "loss":
                    L["injected_loss"] += 1
                elif ev["v"] == "cor":
                    L["injected_corrupt"] += 1
                elif ev["v"] == "dup":
                    L["injected_dup"] += 1
                if not ctl and ev["v"] != "loss" and "dl" in ev:
                    L["delays_ms"].append(int(ev["dl"]) / 1000.0)
    out_links = []
    for L in sorted(links.values(), key=lambda x: x["link"]):
        n = L["arrivals"]
        # Bursty (Gilbert-Elliott) losses are correlated: shrink the sample to its effective size
        # n_eff = n (1 - rho) / (1 + rho), rho = 1 - p_gb - p_bg (lag-1 state autocorrelation).
        rho = L.pop("rho_sum") / n if n else 0.0
        n_eff = n * (1 - rho) / (1 + rho) if rho > 0 else n
        p_hat = L["injected_loss"] / n if n else 0.0
        lo, hi = wilson(round(p_hat * n_eff), max(1, round(n_eff))) if n else (0.0, 1.0)
        exp = L["expected_loss_sum"] / n if n else 0.0
        d = L.pop("delays_ms")
        L.pop("expected_loss_sum")
        # Two-sided z-test of "the realised rate is consistent with the configured model".
        if 0 < exp < 1 and n_eff > 0:
            z = (p_hat - exp) / math.sqrt(exp * (1 - exp) / n_eff)
            p_value = math.erfc(abs(z) / math.sqrt(2))
        else:
            z, p_value = 0.0, 1.0 if abs(p_hat - exp) < 1e-12 else 0.0
        L.update(measured_loss=p_hat, expected_loss=exp, ci95=[lo, hi], effective_n=round(n_eff),
                 ci_method="wilson" if rho == 0 else "wilson, burst-corrected", z=z, p_value=p_value,
                 within_ci=p_value >= 0.01, injected_delay_ms=describe(d) if d else {"n": 0})
        out_links.append(L)
    return {"arrivals_checked": total, "matching": match, "conformance": (match / total) if total else 1.0,
            "mismatches": mismatches, "links": out_links}


# ---------------- main analysis ----------------
def analyze(run_dir: str | pathlib.Path, write: bool = True) -> dict:
    run_dir = pathlib.Path(run_dir)
    s, meta, events = load_run(run_dir)
    seed = int(meta.get("seed", s.seed))
    by_node: Dict[int, List[dict]] = defaultdict(list)
    for e in events:
        by_node[int(e["node"])].append(e)

    clocks = {int(k): ClockMap(v) for k, v in meta.get("sync", {}).items()}

    def host_ns(ev: dict) -> float:
        cm = clocks.get(int(ev["node"]))
        if cm and cm.valid and "t" in ev:
            return cm.to_host_ns(float(ev["t"]))
        return float(ev["host"])  # fall back to serial arrival time

    t0 = float(meta.get("t0_host_ns", min((e["host"] for e in events), default=0)))
    t_stop = float(meta.get("t_stop_host_ns", max((e["host"] for e in events), default=t0)))
    win_start = t0 + s.warmup_s * 1e9
    win_s = max(1e-9, (t_stop - win_start) / 1e9)
    routes = s.routes()

    # index packet-level events: (origin, flow, seq, type) -> list of events
    pk: Dict[tuple, List[dict]] = defaultdict(list)
    for e in events:
        if e.get("e") in ("arr", "drop", "fwd") and "o" in e and not e.get("ctl"):
            pk[(int(e["o"]), int(e["f"]), int(e["s"]), int(e.get("ty", 1)))].append(e)

    down_intervals = _down_intervals(by_node, host_ns, t_stop)
    link_down = _down_intervals(by_node, host_ns, t_stop, down_ev="link")

    flows_out = []
    series = {}
    for f in s.flows:
        tx = {}
        for e in by_node[f.src]:
            if e.get("e") == "tx" and int(e["f"]) == f.id:
                tx[int(e["s"])] = (host_ns(e), bool(e.get("ok", 1)), int(e.get("l", 0)))
        window = {q: v for q, v in tx.items() if win_start <= v[0] <= t_stop}
        rx: Dict[int, float] = {}
        dups = 0
        for e in by_node[f.dst]:
            if e.get("e") == "rx" and int(e["f"]) == f.id and int(e["o"]) == f.src:
                q = int(e["s"])
                if q in rx:
                    dups += 1
                else:
                    rx[q] = host_ns(e)
        rtt: Dict[int, float] = {}
        for e in by_node[f.src]:
            if e.get("e") == "rtt" and int(e["f"]) == f.id and int(e["s"]) not in rtt:
                rtt[int(e["s"])] = int(e["rtt"]) / 1000.0
        sent = len(window)
        delivered = [q for q in window if q in rx]
        rtts = [rtt[q] for q in sorted(window) if q in rtt]
        owd = [(rx[q] - window[q][0]) / 1e6 for q in sorted(window) if q in rx]
        fwd_path = topo.path(routes, f.src, f.dst) if s.topology.enforce else [f.src, f.dst]
        # attribution of every lost packet (forward direction)
        causes: Dict[str, int] = defaultdict(int)
        where: Dict[str, int] = defaultdict(int)
        for q, (t_tx, ok, _) in window.items():
            if q in rx:
                continue
            cause, at = _blame(pk.get((f.src, f.id, q, 1), []), ok, t_tx, fwd_path, down_intervals, link_down)
            causes[cause] += 1
            if at:
                where[at] += 1
        echo_lost = sum(1 for q in delivered if q not in rtt) if f.echo else 0
        payload = f.size
        flows_out.append({
            "id": f.id, "src": f.src, "dst": f.dst, "path": fwd_path, "hops": (len(fwd_path) - 1) if fwd_path else None,
            "sent": sent, "delivered": len(delivered), "pdr": len(delivered) / sent if sent else None,
            "duplicates": dups, "echo_replies": len(rtts), "echo_success": (len(rtts) / sent) if (sent and f.echo) else None,
            "echo_lost_on_return": echo_lost,
            "rtt_ms": describe(rtts), "owd_ms": describe(owd),
            "goodput_bps": len(delivered) * payload * 8 / win_s,
            "loss_causes": dict(causes), "loss_location": dict(where),
        })
        series[str(f.id)] = _series(window, rx, rtt, t0, s.duration_s)

    recovery = _recovery(s, meta, by_node, host_ns, routes)
    aud = audit(seed, by_node)
    links = _link_delivery(by_node, aud)

    node_stats = {}
    for nid, evs in by_node.items():
        last = {}
        for e in evs:
            if e.get("e") in ("stats", "tstats"):
                last[e["e"]] = {k: v for k, v in e.items() if k not in ("node", "host", "e", "t")}
        last["txerr"] = sum(1 for e in evs if e.get("e") == "txerr")
        last["link_events"] = [{"t_s": (host_ns(e) - t0) / 1e9, "up": e.get("up")} for e in evs if e.get("e") == "link"]
        node_stats[str(nid)] = last

    pdrs = [x["pdr"] for x in flows_out if x["pdr"] is not None]
    all_rtt = [v for x in flows_out for v in ([x["rtt_ms"].get("mean")] if x["rtt_ms"].get("n") else [])]
    summary = {
        "scenario": s.name, "transport": s.transport.name, "topology": s.topology.preset, "seed": str(seed),
        "nodes": len(s.nodes), "flows": len(s.flows), "window_s": win_s,
        "pdr_mean": st.mean(pdrs) if pdrs else None, "pdr_min": min(pdrs) if pdrs else None,
        "rtt_mean_ms": st.mean(all_rtt) if all_rtt else None,
        "goodput_bps_total": sum(x["goodput_bps"] for x in flows_out),
        "fault_conformance": aud["conformance"],
        "recovery_s_mean": _mean_or_none([r["recovery_s"] for r in recovery if r.get("recovery_s") is not None]),
        "stopped_early": meta.get("stopped_early", False),
    }
    result = {
        "summary": summary, "flows": flows_out, "links": links, "audit": aud, "recovery": recovery,
        "nodes": node_stats, "series": series,
        "clock": {str(k): {"valid": c.valid, "drift_ppm": c.drift_ppm() if c.valid else None,
                           "sync_error_bound_us": c.err_us} for k, c in clocks.items()},
    }
    if write:
        (run_dir / "metrics.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def _mean_or_none(xs: List[float]) -> Optional[float]:
    return st.mean(xs) if xs else None


def _down_intervals(by_node, host_ns, t_end, down_ev: str = "node") -> Dict[int, List[Tuple[float, float]]]:
    """Intervals where a node was failed (down_ev="node") or its transport link was down (down_ev="link")."""
    out: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    for nid, evs in by_node.items():
        start = None
        seen_up = False
        for e in evs:
            kind = e.get("e")
            if down_ev == "node":
                is_down, is_up = kind == "down", kind == "up"
            else:
                is_down = kind == "link" and not e.get("up") and seen_up
                is_up = kind == "link" and bool(e.get("up"))
                seen_up = seen_up or is_up
            if is_down and start is None:
                start = host_ns(e)
            elif is_up and start is not None:
                out[nid].append((start, host_ns(e)))
                start = None
        if start is not None:
            out[nid].append((start, t_end + 1e12))
    return out


def _blame(evs: List[dict], tx_ok: bool, t_tx: float, path, down, link_down) -> Tuple[str, Optional[str]]:
    if not tx_ok:
        return "send_fail", None
    for e in evs:
        if e.get("e") == "arr" and e.get("v") == "loss":
            return "injected_loss", f"{e['src']}->{e['node']}"
        if e.get("e") == "arr" and e.get("v") == "cor":
            return "injected_corrupt", f"{e['src']}->{e['node']}"
    for e in evs:
        if e.get("e") == "drop":
            return f"drop_{e.get('why')}", str(e["node"])
        if e.get("e") == "fwd" and not e.get("ok", 1):
            return "send_fail", str(e["node"])
    for nid in (path or []):
        for a, b in down.get(nid, []):
            if a <= t_tx + 2e9 and t_tx <= b:  # the packet could still have been in flight
                return "node_down", str(nid)
    for nid in (path or []):
        for a, b in link_down.get(nid, []):
            if a <= t_tx + 2e9 and t_tx <= b:
                return "link_down", str(nid)  # e.g. Wi-Fi re-association / broker reconnect in progress
    return "channel", None


def _series(window, rx, rtt, t0, duration_s) -> dict:
    bins = max(1, int(math.ceil(duration_s)))
    sent = [0] * bins
    got = [0] * bins
    pts = []
    for q, (t, _, _) in window.items():
        b = int((t - t0) / 1e9)
        if 0 <= b < bins:
            sent[b] += 1
            got[b] += q in rx
    for q in sorted(window)[:5000]:
        if q in rtt:
            pts.append([round((window[q][0] - t0) / 1e9, 4), round(rtt[q], 3)])
    return {"pdr_per_s": [(g / n if n else None) for g, n in zip(got, sent)], "sent_per_s": sent, "rtt_points": pts}


def _recovery(s: Scenario, meta, by_node, host_ns, routes) -> List[dict]:
    """For every disruption that ends (revive / fault cleared), time until each affected flow delivers again."""
    tl = meta.get("timeline", [])
    disruptions = []
    for i, ev in enumerate(tl):
        if ev["action"] == "kill":
            end = next((x for x in tl[i + 1:] if x["action"] == "revive" and x.get("node") == ev.get("node")), None)
            disruptions.append(dict(kind="node", node=ev["node"], hard=ev.get("hard", False), start=ev["host_ns"],
                                    end=end["host_ns"] if end else None))
        elif ev["action"] == "fault" and ev.get("link") not in (None, "default"):
            end = next((x for x in tl[i + 1:] if x.get("link") == ev["link"]), None)
            disruptions.append(dict(kind="link", link=ev["link"], start=ev["host_ns"], end=end["host_ns"] if end else None))
    out = []
    for d in disruptions:
        for f in s.flows:
            p = topo.path(routes, f.src, f.dst) if s.topology.enforce else [f.src, f.dst]
            rp = topo.path(routes, f.dst, f.src) if s.topology.enforce else [f.dst, f.src]
            if d["kind"] == "node":
                affected = d["node"] in (p or [])
            else:
                a, b = parse_link(d["link"])
                hops = list(zip(p or [], (p or [])[1:])) + list(zip(rp or [], (rp or [])[1:]))
                affected = (a, b) in hops
            if not affected:
                continue
            rx_times = sorted(host_ns(e) for e in by_node[f.dst]
                              if e.get("e") == "rx" and int(e["f"]) == f.id and int(e["o"]) == f.src)
            before = [t for t in rx_times if t < d["start"]]
            rec = {"disruption": d["kind"], "target": d.get("node", d.get("link")), "hard": d.get("hard"),
                   "flow": f.id, "start_s": (d["start"] - meta["t0_host_ns"]) / 1e9,
                   "end_s": (d["end"] - meta["t0_host_ns"]) / 1e9 if d["end"] else None, "recovery_s": None,
                   "outage_s": None, "relink_s": None}
            if d["end"]:
                after = [t for t in rx_times if t >= d["end"]]
                if after:
                    rec["recovery_s"] = (after[0] - d["end"]) / 1e9
                    if before:
                        rec["outage_s"] = (after[0] - before[-1]) / 1e9
                if d["kind"] == "node" and d.get("hard"):
                    ups = [host_ns(e) for e in by_node[d["node"]] if e.get("e") == "link" and e.get("up")
                           and host_ns(e) >= d["end"] - 1e6]
                    if ups:
                        rec["relink_s"] = max(0.0, (ups[0] - d["end"]) / 1e9)
            out.append(rec)
    return out


def _link_delivery(by_node, aud) -> List[dict]:
    """Per directed hop: frames handed to the transport vs frames that reached the receiver's fault engine."""
    sent: Dict[str, int] = defaultdict(int)
    for nid, evs in by_node.items():
        for e in evs:
            if e.get("e") in ("tx", "fwd", "etx") and e.get("ok", 1) and "n" in e:
                sent[f"{nid}->{int(e['n'])}"] += 1
    arrived: Dict[str, int] = defaultdict(int)
    for nid, evs in by_node.items():
        for e in evs:
            if e.get("e") == "arr" and not e.get("ctl"):
                arrived[f"{int(e['src'])}->{nid}"] += 1
    inj = {L["link"]: L for L in aud["links"]}
    out = []
    for k in sorted(set(sent) | set(arrived)):
        n_s, n_a = sent.get(k, 0), arrived.get(k, 0)
        L = inj.get(k, {})
        out.append({"link": k, "frames_sent": n_s, "frames_arrived": n_a,
                    "channel_delivery": min(1.0, n_a / n_s) if n_s else None,
                    "retransmission_surplus": max(0, n_a - n_s),
                    "injected_loss": L.get("injected_loss", 0), "measured_injected_loss": L.get("measured_loss"),
                    "expected_injected_loss": L.get("expected_loss"), "within_ci": L.get("within_ci")})
    return out


# ---------------- campaign summary ----------------
def summarize_campaign(root: str | pathlib.Path) -> dict:
    root = pathlib.Path(root)
    rows = []
    for mfile in sorted(root.glob("v*/rep*/metrics.json")):
        m = json.loads(mfile.read_text(encoding="utf-8"))
        sc = yaml.safe_load((mfile.parent / "scenario.yaml").read_text(encoding="utf-8"))
        for f in m["flows"]:
            rows.append({"variant": mfile.parent.parent.name, "variant_name": sc.get("name"), "rep": mfile.parent.name,
                         "transport": m["summary"]["transport"], "flow": f["id"], "hops": f["hops"], "sent": f["sent"],
                         "pdr": f["pdr"], "rtt_mean_ms": f["rtt_ms"].get("mean"), "rtt_p95_ms": f["rtt_ms"].get("p95"),
                         "owd_mean_ms": f["owd_ms"].get("mean"), "goodput_bps": f["goodput_bps"],
                         "injected_loss": f["loss_causes"].get("injected_loss", 0),
                         "channel_loss": f["loss_causes"].get("channel", 0),
                         "conformance": m["audit"]["conformance"],
                         "recovery_s": _mean_or_none([r["recovery_s"] for r in m["recovery"]
                                                      if r["flow"] == f["id"] and r["recovery_s"] is not None])})
    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["variant"], r["flow"])].append(r)
    agg = []
    for (variant, flow), rs in sorted(groups.items()):
        a = {"variant": variant, "variant_name": rs[0]["variant_name"], "transport": rs[0]["transport"], "flow": flow,
             "reps": len(rs)}
        for key in ("pdr", "rtt_mean_ms", "rtt_p95_ms", "goodput_bps", "recovery_s"):
            xs = [r[key] for r in rs if r[key] is not None]
            if xs:
                mu = st.mean(xs)
                sd = st.stdev(xs) if len(xs) > 1 else 0.0
                a[key] = {"mean": mu, "std": sd, "cv": (sd / mu) if mu else None,
                          "ci95": 1.96 * sd / math.sqrt(len(xs)) if len(xs) > 1 else None}
        a["conformance_min"] = min(r["conformance"] for r in rs)
        agg.append(a)
    out = {"runs": rows, "aggregate": agg}
    (root / "campaign.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    if rows:
        with open(root / "campaign.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return out
