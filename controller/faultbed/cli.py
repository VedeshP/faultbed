"""Command line entry point.

    faultbed boards                         list connected boards
    faultbed run scenarios/x.yaml [--sim N] run a scenario (campaign) and analyse it
    faultbed analyze results/<run>/v00/rep01
    faultbed serve [--sim N]                web UI on http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import os
import json
import pathlib
import sys

from . import metrics as M
from . import scenario as S
from .runner import Runner, Testbed


def make_testbed(sim: int, ports=None, baud: int = 921600, sim_seed: int = 0, sim_speed: float = 0.0) -> Testbed:
    if sim:
        from .sim import SimWorld

        world = SimWorld(seed=sim_seed, realtime=sim_speed)
        tb = Testbed(clock=world)
        tb.attach(world.add_boards(sim))
    else:
        tb = Testbed()
        tb.attach_serial(ports, baud)
    return tb


def cmd_boards(a) -> int:
    tb = make_testbed(a.sim, a.port, a.baud)
    found = tb.discover(timeout=4)
    if not found:
        print("no boards answered. Is the FaultBed firmware flashed? Try --port COM5")
    for b in found:
        print(f"{b.port.name:12s} {b.mac:18s} {b.chip:10s} fw={b.fw:10s} transports={','.join(b.transports)}")
    tb.detach_all()
    return 0 if found else 1


def _print_summary(m: dict) -> None:
    s = m["summary"]
    print(f"\n== {s['scenario']}  transport={s['transport']}  topology={s['topology']}  seed={s['seed']}")
    for f in m["flows"]:
        r = f["rtt_ms"]
        pdr = f"{f['pdr'] * 100:6.2f}%" if f["pdr"] is not None else "   n/a"
        rtt = f"rtt mean {r['mean']:7.2f} ms p95 {r['p95']:7.2f}" if r.get("n") else "rtt n/a"
        print(f"  flow {f['id']:>3} {f['src']}->{f['dst']} hops={f['hops']}  sent={f['sent']:5d} PDR={pdr}  {rtt}"
              f"  losses={f['loss_causes']}")
    a = m["audit"]
    print(f"  fault audit: {a['matching']}/{a['arrivals_checked']} arrivals conform ({a['conformance'] * 100:.2f}%)")
    for L in a["links"]:
        if L["expected_loss"] > 0 or L["injected_loss"]:
            print(f"    link {L['link']:>6}: injected {L['measured_loss'] * 100:6.2f}% expected {L['expected_loss'] * 100:6.2f}%"
                  f"  95% CI [{L['ci95'][0] * 100:.2f}, {L['ci95'][1] * 100:.2f}]  p={L['p_value']:.3f}"
                  f"  {'consistent' if L['within_ci'] else 'INCONSISTENT'}")
    for r in m["recovery"]:
        if r["recovery_s"] is not None:
            print(f"  recovery after {r['disruption']} {r['target']}: flow {r['flow']} back in {r['recovery_s']:.3f} s")


def cmd_run(a) -> int:
    sc = S.load(a.scenario)
    tb = make_testbed(a.sim, a.port, a.baud, a.sim_seed)
    runner = Runner(tb, a.out)
    runner.on_progress = lambda p: print(f"[{p.run_index}/{p.run_total}] {p.variant} rep {p.rep}: {p.phase}",
                                         file=sys.stderr) if p.phase in ("running", "analyzing") else None
    root = runner.run_campaign(sc)
    for mf in sorted(root.glob("v*/rep*/metrics.json")):
        _print_summary(json.loads(mf.read_text(encoding="utf-8")))
    print(f"\nresults: {root}")
    tb.detach_all()
    return 0


def cmd_analyze(a) -> int:
    p = pathlib.Path(a.run_dir)
    if (p / "events.jsonl").exists():
        _print_summary(M.analyze(p))
    else:
        out = M.summarize_campaign(p)
        print(json.dumps(out["aggregate"], indent=1))
    return 0


def cmd_serve(a) -> int:
    from .web.server import serve

    serve(host=a.host, port=a.http_port, sim=a.sim, ports=a.port, baud=a.baud, results=a.out,
          scenarios=a.scenarios, sim_speed=a.sim_speed)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="faultbed", description="FaultBed controller")
    ap.add_argument("--sim", type=int, default=0, metavar="N", help="use N simulated boards instead of hardware")
    ap.add_argument("--sim-seed", type=int, default=0)
    ap.add_argument("--port", action="append", help="serial port (repeatable); default: auto-detect")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--out", default="results", help="results directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("boards").set_defaults(fn=cmd_boards)
    r = sub.add_parser("run")
    r.add_argument("scenario")
    r.set_defaults(fn=cmd_run)
    an = sub.add_parser("analyze")
    an.add_argument("run_dir")
    an.set_defaults(fn=cmd_analyze)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--http-port", type=int, default=int(os.environ.get("PORT", 8000)))
    sv.add_argument("--scenarios", default="scenarios")
    sv.add_argument("--sim-speed", type=float, default=1.0, help="simulated time per wall second (0 = unpaced)")
    sv.set_defaults(fn=cmd_serve)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
