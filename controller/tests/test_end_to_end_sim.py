"""Whole pipeline on simulated boards: configure -> run -> log -> analyse."""
import json

import pytest

from faultbed import metrics as M
from faultbed.cli import make_testbed
from faultbed.runner import Runner
from faultbed.scenario import Scenario

BASE = dict(nodes=[{"id": i} for i in range(1, 5)], topology={"preset": "line"},
            flows=[{"id": 1, "src": 1, "dst": 4, "interval_ms": 50, "size": 32}], duration_s=20, warmup_s=1, drain_s=2)


def run(tmp_path, sim_seed=0, **over):
    s = Scenario(**{**BASE, **over})
    tb = make_testbed(len(s.nodes), sim_seed=sim_seed)
    root = Runner(tb, tmp_path).run_campaign(s)
    ms = [json.loads(p.read_text()) for p in sorted(root.glob("v*/rep*/metrics.json"))]
    return root, ms


@pytest.mark.parametrize("transport", ["espnow", "udp", "mqtt", "coap"])
def test_all_transports_baseline(tmp_path, transport):
    _, (m,) = run(tmp_path, transport={"name": transport})
    f = m["flows"][0]
    assert f["sent"] > 300
    assert f["pdr"] > 0.9
    assert f["hops"] == 3
    assert f["rtt_ms"]["n"] > 0
    assert m["audit"]["conformance"] == 1.0


def test_injected_loss_is_accurate_and_attributed(tmp_path):
    _, (m,) = run(tmp_path, faults={"links": {"2->3": {"model": "bernoulli", "p_loss": 0.3}}}, duration_s=40)
    link = next(L for L in m["audit"]["links"] if L["link"] == "2->3")
    assert link["within_ci"] and link["p_value"] >= 0.01   # consistent with p=0.3 at the 1% level
    assert abs(link["measured_loss"] - 0.3) < 0.05
    f = m["flows"][0]
    assert f["loss_causes"].get("injected_loss", 0) > 0
    assert f["loss_location"].get("2->3", 0) == f["loss_causes"]["injected_loss"]
    assert m["audit"]["conformance"] == 1.0


def test_same_seed_same_faults_despite_different_channel(tmp_path):
    """The reproducibility claim: the k-th frame on a link meets the same fate in every run with the same seed,
    even when the (simulated) radio behaves differently between runs."""
    faults = {"default": {"model": "bernoulli", "p_loss": 0.2, "delay_ms": 3, "jitter_ms": 4}}

    def verdicts(root):
        _, _, ev = M.load_run(next(root.glob("v*/rep*")))
        out = {}
        for e in ev:
            if e["e"] == "arr":
                out[(e["node"], e["src"], e["i"])] = (e["v"], e.get("dl"))
        return out

    r1, _ = run(tmp_path / "a", sim_seed=1, faults=faults)
    r2, _ = run(tmp_path / "b", sim_seed=2, faults=faults)
    v1, v2 = verdicts(r1), verdicts(r2)
    common = set(v1) & set(v2)
    assert len(common) > 500
    assert all(v1[k] == v2[k] for k in common)


def test_node_failure_recovery(tmp_path):
    _, (m,) = run(tmp_path, transport={"name": "udp"}, duration_s=30,
                  timeline=[{"at_s": 8, "action": "kill", "node": 2, "hard": True},
                            {"at_s": 15, "action": "revive", "node": 2}])
    f = m["flows"][0]
    assert f["loss_causes"].get("node_down", 0) > 50
    (rec,) = [r for r in m["recovery"] if r["flow"] == 1]
    assert rec["recovery_s"] is not None and 0 < rec["recovery_s"] < 6
    assert rec["relink_s"] is not None


def test_campaign_summary(tmp_path):
    root, ms = run(tmp_path, repeats=2, duration_s=8, sweep={"transport.name": ["espnow", "mqtt"]})
    assert len(ms) == 4
    c = json.loads((root / "campaign.json").read_text())
    assert len(c["aggregate"]) == 2
    assert all(a["reps"] == 2 and "pdr" in a for a in c["aggregate"])
    assert (root / "campaign.csv").exists()


def test_clock_map_recovers_offset_and_drift():
    off_us, drift = 12_345_678, 25e-6
    samples = []
    for t in list(range(0, 400_000_000, 20_000_000)) + list(range(30_000_000_000, 30_400_000_000, 20_000_000)):
        rtt = 1_000_000 + (t // 7) % 900_000  # 1-1.9 ms serial round trip
        mid = t + rtt / 2
        samples.append((t, t + rtt, int(mid / 1000 * (1 + drift) + off_us)))
    cm = M.ClockMap(samples)
    assert abs(cm.drift_ppm() - 25) < 1
    probe = 15_000_000_000
    assert abs(cm.to_host_ns(probe / 1000 * (1 + drift) + off_us) - probe) < 50_000  # < 50 us
