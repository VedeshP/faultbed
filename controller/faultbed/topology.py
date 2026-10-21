"""Virtual topologies over physically co-located nodes.

All boards sit within radio range of each other; the controller decides who is
allowed to hear whom (each node drops frames from non-neighbours) and installs
shortest-path routes. So the same 4-5 boards on a desk can be a line, ring,
star, tree, mesh or any custom graph - changed in software between runs.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, List, Optional, Set, Tuple

Edge = Tuple[int, int]
PRESETS = ("full", "line", "ring", "star", "tree", "grid", "custom")


def build_edges(preset: str, ids: List[int], center: Optional[int] = None,
                custom: Iterable[Iterable[int]] = ()) -> Set[Edge]:
    ids = sorted(ids)
    n = len(ids)
    e: Set[Edge] = set()

    def add(a: int, b: int) -> None:
        if a != b:
            e.add((min(a, b), max(a, b)))

    if preset == "full":
        for i in range(n):
            for j in range(i + 1, n):
                add(ids[i], ids[j])
    elif preset == "line":
        for i in range(n - 1):
            add(ids[i], ids[i + 1])
    elif preset == "ring":
        for i in range(n):
            add(ids[i], ids[(i + 1) % n])
    elif preset == "star":
        c = center if center in ids else ids[0]
        for x in ids:
            add(c, x)
    elif preset == "tree":  # binary tree in id order: parent of k is (k-1)//2
        for k in range(1, n):
            add(ids[(k - 1) // 2], ids[k])
    elif preset == "grid":
        cols = max(1, round(n ** 0.5))
        for k in range(n):
            if (k + 1) % cols and k + 1 < n:
                add(ids[k], ids[k + 1])
            if k + cols < n:
                add(ids[k], ids[k + cols])
    elif preset == "custom":
        for pair in custom:
            a, b = list(pair)[:2]
            if a in ids and b in ids:
                add(int(a), int(b))
    else:
        raise ValueError(f"unknown topology preset {preset!r}; expected one of {PRESETS}")
    return e


def neighbors(edges: Set[Edge], ids: List[int]) -> Dict[int, List[int]]:
    nb: Dict[int, Set[int]] = {i: set() for i in ids}
    for a, b in edges:
        nb[a].add(b)
        nb[b].add(a)
    return {i: sorted(v) for i, v in nb.items()}


def shortest_path_routes(edges: Set[Edge], ids: List[int]) -> Dict[int, Dict[int, int]]:
    """routes[src][dst] = next hop, via BFS (ties broken by lowest id => deterministic)."""
    nb = neighbors(edges, ids)
    routes: Dict[int, Dict[int, int]] = {}
    for src in ids:
        first_hop: Dict[int, int] = {}
        seen = {src}
        q = deque()
        for n in nb[src]:
            seen.add(n)
            first_hop[n] = n
            q.append(n)
        while q:
            cur = q.popleft()
            for n in nb[cur]:
                if n not in seen:
                    seen.add(n)
                    first_hop[n] = first_hop[cur]
                    q.append(n)
        routes[src] = first_hop
    return routes


def path(routes: Dict[int, Dict[int, int]], src: int, dst: int, limit: int = 32) -> Optional[List[int]]:
    p = [src]
    cur = src
    while cur != dst:
        nxt = routes.get(cur, {}).get(dst)
        if nxt is None or len(p) > limit:
            return None
        p.append(nxt)
        cur = nxt
    return p


def is_connected(edges: Set[Edge], ids: List[int]) -> bool:
    if not ids:
        return True
    return len(shortest_path_routes(edges, ids)[ids[0]]) == len(ids) - 1


def layout(ids: List[int], preset: str) -> Dict[int, Tuple[float, float]]:
    """Default drawing positions in a unit square for the web UI."""
    import math

    ids = sorted(ids)
    n = len(ids)
    if preset == "line":
        return {i: ((k + 0.5) / n, 0.5) for k, i in enumerate(ids)}
    if preset == "grid":
        cols = max(1, round(n ** 0.5))
        rows = math.ceil(n / cols)
        return {i: ((k % cols + 0.5) / cols, (k // cols + 0.5) / rows) for k, i in enumerate(ids)}
    if preset == "star" and n > 1:
        pos = {ids[0]: (0.5, 0.5)}
        for k, i in enumerate(ids[1:]):
            a = 2 * math.pi * k / (n - 1)
            pos[i] = (0.5 + 0.38 * math.cos(a), 0.5 + 0.38 * math.sin(a))
        return pos
    return {i: (0.5 + 0.4 * math.cos(2 * math.pi * k / max(n, 1) - math.pi / 2),
                0.5 + 0.4 * math.sin(2 * math.pi * k / max(n, 1) - math.pi / 2)) for k, i in enumerate(ids)}
