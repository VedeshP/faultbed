"""The Python ports must be bit-exact with the firmware core."""
from faultbed import hashing as H
from faultbed import packet as P
from faultbed.faults import FaultEngine, LinkRule

# Printed by firmware/test/native/test_core.cpp (print_vectors)
CPP_DRAWS = [5310043328599897433, 3427198334718195943, 10502662551747990045]
CPP_THR_03 = 2702159776422297


def test_draw_vectors_match_firmware():
    assert [H.draw(42, 1, 2, H.STREAM_LOSS, i) for i in range(3)] == CPP_DRAWS
    assert H.prob_threshold(0.3) == CPP_THR_03
    assert H.prob_threshold(0) == 0 and H.prob_threshold(1) == 1 << 53


def test_crc_and_frames():
    assert P.crc16(b"123456789") == 0x29B1
    h = P.Header(origin=1, final_dst=3, hop_src=1, hop_dst=2, flow=4, seq=123456, origin_us=0xDEADBEEF, payload_len=10)
    f = P.encode(h)
    ok, d = P.decode(f)
    assert ok and d.seq == 123456 and d.origin_us == 0xDEADBEEF and d.hop_dst == 2
    assert all(not P.decode(P.flip_bit(f, b))[0] for b in range(len(f) * 8))
    ok, d = P.decode(P.rewrite_hop(f, 2, 3, 7, 1))
    assert ok and (d.hop_src, d.hop_dst, d.ttl, d.hops) == (2, 3, 7, 1)


def test_bernoulli_accuracy():
    for p in (0.05, 0.3, 0.7):
        e = FaultEngine(1234, 2)
        e.set_default_rule(LinkRule(model="bernoulli", p_loss=p))
        n = 40000
        lost = sum(e.decide(1, 40).lost for _ in range(n))
        assert abs(lost / n - p) < 0.01


def test_links_are_independent_and_seeded():
    a, b = FaultEngine(7, 2), FaultEngine(7, 2)
    for e in (a, b):
        e.set_default_rule(LinkRule(model="bernoulli", p_loss=0.4))
    for _ in range(300):
        a.decide(1, 40)  # extra traffic on another link must not shift link 3's decisions
    assert [a.decide(3, 40).lost for _ in range(500)] == [b.decide(3, 40).lost for _ in range(500)]
    c = FaultEngine(8, 2)
    c.set_default_rule(LinkRule(model="bernoulli", p_loss=0.4))
    assert [c.decide(3, 40).lost for _ in range(500)] != [FaultEngine(7, 2).decide(3, 40).lost for _ in range(500)]


def test_gilbert_elliott_stationary_rate():
    r = LinkRule(model="gilbert_elliott", ge_p_gb=0.05, ge_p_bg=0.25, ge_loss_good=0.01, ge_loss_bad=0.8)
    e = FaultEngine(99, 2)
    e.set_default_rule(r)
    n = 100000
    lost = sum(e.decide(1, 40).lost for _ in range(n))
    assert abs(lost / n - r.expected_loss()) < 0.012


def test_delay_and_jitter_bounds():
    e = FaultEngine(5, 2)
    e.set_default_rule(LinkRule(delay_ms=5, jitter_ms=2))
    ds = [e.decide(1, 40).delay_us for _ in range(5000)]
    assert min(ds) >= 5000 and max(ds) <= 7000
