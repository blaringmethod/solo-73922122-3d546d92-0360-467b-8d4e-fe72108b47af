"""Pure graph algorithms for stratigraphic precedence.

The service stores precedence as a directed graph (earlier -> later).  A valid
site-wide ordering is exactly a topological ordering of that graph.  The
earliest/latest possible one-based position of a node can therefore be computed
from reachability only:

    min_rank(v) = 1 + |ancestors(v)|
    max_rank(v) = n - |descendants(v)|

No permutation enumeration is needed.  Witness orderings are constructed by
filtering one topological order, which is valid because the ancestor set is
downward closed and the descendant set is upward closed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from heapq import heapify, heappop, heappush
from typing import Iterable, Mapping


class CyclicGraphError(ValueError):
    """Raised when a topological ordering is requested for a cyclic graph."""

    def __init__(self, cycle_nodes: list[int]):
        self.cycle_nodes = cycle_nodes
        super().__init__(f"graph contains a cycle through nodes {cycle_nodes}")


@dataclass(frozen=True)
class RankResult:
    min_rank: int
    max_rank: int
    ancestors: frozenset[int]
    descendants: frozenset[int]
    min_order: list[int]
    max_order: list[int]


def build_adjacency(
    nodes: Iterable[int], edges: Iterable[tuple[int, int]]
) -> dict[int, set[int]]:
    """Return a deterministic adjacency map for ``nodes``/``edges``."""

    node_set = set(nodes)
    adjacency: dict[int, set[int]] = {node: set() for node in node_set}
    for earlier, later in edges:
        if earlier not in node_set or later not in node_set:
            raise ValueError(
                f"edge ({earlier}, {later}) references a node outside the graph"
            )
        adjacency[earlier].add(later)
    return adjacency


def reverse_adjacency(adjacency: Mapping[int, set[int]]) -> dict[int, set[int]]:
    reverse: dict[int, set[int]] = {node: set() for node in adjacency}
    for earlier, laters in adjacency.items():
        for later in laters:
            reverse[later].add(earlier)
    return reverse


def reachable(start: int, adjacency: Mapping[int, set[int]]) -> frozenset[int]:
    """All nodes reachable from ``start`` (excluding ``start`` itself)."""

    seen: set[int] = set()
    queue = deque(adjacency.get(start, ()))
    while queue:
        node = queue.popleft()
        if node == start or node in seen:
            continue
        seen.add(node)
        queue.extend(adjacency.get(node, ()))
    return frozenset(seen)


def find_cycle(
    nodes: Iterable[int], adjacency: Mapping[int, set[int]]
) -> list[int] | None:
    """Return one simple cycle as ``[v0, v1, ..., vk]``.

    The edge ``vk -> v0`` closes the cycle.  A self-loop is returned as
    ``[v0]``.  ``None`` means the graph is acyclic.  Iterative DFS keeps the
    function safe for large inventories.
    """

    node_list = sorted(nodes)
    white, gray, black = 0, 1, 2
    color = {node: white for node in node_list}

    for start in node_list:
        if color[start] != white:
            continue
        color[start] = gray
        path = [start]
        position = {start: 0}
        stack: list[tuple[int, Iterable[int]]] = [
            (start, iter(sorted(adjacency.get(start, ()))))
        ]
        while stack:
            node, neighbours = stack[-1]
            advanced = False
            for neighbour in neighbours:
                if color[neighbour] == white:
                    color[neighbour] = gray
                    position[neighbour] = len(path)
                    path.append(neighbour)
                    stack.append(
                        (neighbour, iter(sorted(adjacency.get(neighbour, ()))))
                    )
                    advanced = True
                    break
                if color[neighbour] == gray:
                    return path[position[neighbour] :]
            if not advanced:
                color[node] = black
                stack.pop()
                path.pop()
                position.pop(node, None)
    return None


def topological_order(
    nodes: Iterable[int], adjacency: Mapping[int, set[int]]
) -> list[int] | None:
    """Deterministic Kahn topological sort, or ``None`` if cyclic."""

    node_list = sorted(nodes)
    indegree = {node: 0 for node in node_list}
    for earlier in node_list:
        for later in adjacency.get(earlier, ()):
            indegree[later] += 1

    heap = [node for node in node_list if indegree[node] == 0]
    heapify(heap)
    order: list[int] = []
    while heap:
        node = heappop(heap)
        order.append(node)
        for later in sorted(adjacency.get(node, ())):
            indegree[later] -= 1
            if indegree[later] == 0:
                heappush(heap, later)
    if len(order) != len(node_list):
        return None
    return order


def compute_rank(
    nodes: Iterable[int],
    edges: Iterable[tuple[int, int]],
    target: int,
) -> RankResult:
    """Compute possible rank bounds and witness orderings for ``target``."""

    node_list = sorted(nodes)
    if target not in node_list:
        raise ValueError(f"target node {target} is not in the graph")

    adjacency = build_adjacency(node_list, edges)
    reverse = reverse_adjacency(adjacency)

    cycle = find_cycle(node_list, adjacency)
    if cycle is not None:
        raise CyclicGraphError(cycle)

    ancestors = reachable(target, reverse)
    descendants = reachable(target, adjacency)
    order = topological_order(node_list, adjacency)
    if order is None:  # pragma: no cover - guarded by the cycle check above
        raise CyclicGraphError(find_cycle(node_list, adjacency) or [target])

    # Ancestors are downward closed, so filtering preserves topological order.
    min_order = (
        [node for node in order if node in ancestors]
        + [target]
        + [node for node in order if node not in ancestors and node != target]
    )
    # Descendants are upward closed; everything else can precede the target.
    max_order = (
        [node for node in order if node not in descendants and node != target]
        + [target]
        + [node for node in order if node in descendants]
    )

    return RankResult(
        min_rank=len(ancestors) + 1,
        max_rank=len(node_list) - len(descendants),
        ancestors=ancestors,
        descendants=descendants,
        min_order=min_order,
        max_order=max_order,
    )


def validate_order(
    nodes: Iterable[int],
    edges: Iterable[tuple[int, int]],
    order: Iterable[int],
) -> bool:
    """Return whether ``order`` is a valid permutation respecting every edge."""

    node_list = list(nodes)
    order_list = list(order)
    if len(order_list) != len(node_list) or set(order_list) != set(node_list):
        return False
    position = {node: index for index, node in enumerate(order_list)}
    return all(position[earlier] < position[later] for earlier, later in edges)
