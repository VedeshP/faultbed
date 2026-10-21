import pytest
from pydantic import ValidationError

from faultbed import topology as T
from faultbed.scenario import Scenario


def nodes(n):
    return [{"id": i} for i in range(1, n + 1)]


@pytest.mark.parametrize("preset,count", [("full", 10), ("line", 4), ("ring", 5), ("star", 4), ("tree", 4), ("grid", 5)])
def test_presets_connected(preset, count):
    ids = [1, 2, 3, 4, 5]
    e = T.build_edges(preset, ids)
    assert len(e) == count
    assert T.is_connected(e, ids)


def test_line_routes():
    ids = [1, 2, 3, 4]
    r = T.shortest_path_routes(T.build_edges("line", ids), ids)
    assert r[1][4] == 2 and r[4][1] == 3
    assert T.path(r, 1, 4) == [1, 2, 3, 4]


def test_custom_disconnected_rejected():
    with pytest.raises(ValidationError):
        Scenario(nodes=nodes(4), topology={"preset": "custom", "edges": [[1, 2], [3, 4]]})


def test_node_config_contents():
    s = Scenario(nodes=nodes(3), topology={"preset": "line"}, seed=2 ** 63 + 5,
                 faults={"default": {"model": "bernoulli", "p_loss": 0.1}, "links": {"1->2": {"delay_ms": 10}}},
                 flows=[{"id": 1, "src": 1, "dst": 3}])
    c2 = s.node_config(2, s.seed)
    assert c2["neighbors"] == [1, 3]
    assert c2["seed"] == str(2 ** 63 + 5)
    assert c2["faults"]["links"] == {"1": s.faults.links["1->2"].model_dump()}
    assert s.node_config(1, 1)["routes"]["3"] == 2
    assert s.node_config(1, 1)["flows"][0]["dst"] == 3 and "src" not in s.node_config(1, 1)["flows"][0]


def test_validation_errors():
    with pytest.raises(ValidationError):
        Scenario(nodes=nodes(2), flows=[{"id": 1, "src": 1, "dst": 9}])
    with pytest.raises(ValidationError):
        Scenario(nodes=nodes(2), faults={"links": {"1-2": {}}})
    with pytest.raises(ValidationError):
        Scenario(nodes=nodes(2), timeline=[{"at_s": 1, "action": "kill", "node": 7}])
    with pytest.raises(ValidationError):
        Scenario(nodes=nodes(2), faults={"default": {"p_loss": 1.5}})


def test_sweep_expansion():
    s = Scenario(nodes=nodes(2), flows=[{"id": 1, "src": 1, "dst": 2}],
                 sweep={"transport.name": ["espnow", "udp"], "faults.default.p_loss": [0.0, 0.1, 0.2]})
    v = s.expand()
    assert len(v) == 6
    assert {(x.transport.name, x.faults.default.p_loss) for x in v} == {
        (t, p) for t in ("espnow", "udp") for p in (0.0, 0.1, 0.2)}
    assert all(not x.sweep for x in v)
