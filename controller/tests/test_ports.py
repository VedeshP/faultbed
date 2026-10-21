"""SerialNodePort accepts pyserial URLs (rfc2217:// for Wokwi). loop:// echoes writes back, which is enough to
exercise the open / write / line-framing / JSON-parsing path without any device."""
import threading

from faultbed.ports import SerialNodePort


def test_url_port_roundtrip():
    got, done = [], threading.Event()

    def on_event(port, ev, host_ns):
        got.append(ev)
        done.set()

    p = SerialNodePort("loop://")
    p.open(on_event)
    try:
        p.send({"e": "hello", "fw": "x", "transports": ["espnow"]})
        assert done.wait(2)
        assert got[0]["e"] == "hello" and got[0]["transports"] == ["espnow"]
    finally:
        p.close()
