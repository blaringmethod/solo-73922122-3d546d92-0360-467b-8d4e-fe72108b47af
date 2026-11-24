"""Unit tests for the pure graph algorithms."""

from __future__ import annotations

import itertools
import random

import pytest

from app.graph import (
    CyclicGraphError,
    compute_rank,
    find_cycle,
    topological_order,
    validate_order,
)


def _brute_ranks(nodes: list[int], edges: list[tuple[int, int]], target: int):
    ranks = []
    for order in itertools.permutations(nodes):
        if all(order.index(a) < order.index(b) for a, b in edges):
            ranks.append(order.index(target) + 1)
    return min(ranks), max(ranks)


def test_empty_edge_graph_rank_range_covers_every_position():
    nodes = [1, 2, 3, 4]
    result = compute_rank(nodes, [], 2)
    assert (result.min_rank, result.max_rank) == (1, 4)
    assert validate_order(nodes, [], result.min_order)
    assert validate_order(nodes, [], result.max_order)
    assert result.min_order.index(2) == 0
    assert result.max_order.index(2) == 3


def test_chain_rank_is_fixed_and_witnesses_agree():
    nodes = [1, 2, 3, 4, 5]
    edges = [(1, 2), (2, 3), (3, 4), (4, 5)]
    result = compute_rank(nodes, edges, 3)
    assert result.min_rank == result.max_rank == 3
    assert result.min_order == result.max_order == nodes


def test_cross_component_relations_compose():
    nodes = [1, 2, 3, 4, 5, 6]
    edges = [(1, 2), (2, 3), (3, 4), (5, 6), (6, 1)]
    # ancestors of 3: {1,2,5,6}; descendants: {4}
    result = compute_rank(nodes, edges, 3)
    assert result.min_rank == 5
    assert result.max_rank == 5
    assert result.ancestors == frozenset({1, 2, 5, 6})
    assert result.descendants == frozenset({4})


@pytest.mark.parametrize("seed", range(12))
def test_rank_bounds_and_witnesses_match_brute_force(seed: int):
    rng = random.Random(seed)
    count = rng.randint(2, 7)
    nodes = list(range(count))
    edges = []
    for a, b in itertools.permutations(nodes, 2):
        if a < b and rng.random() < 0.32:
            edges.append((a, b))
    target = rng.choice(nodes)
    expected_min, expected_max = _brute_ranks(nodes, edges, target)
    result = compute_rank(nodes, edges, target)
    assert (result.min_rank, result.max_rank) == (expected_min, expected_max)
    assert validate_order(nodes, edges, result.min_order)
    assert validate_order(nodes, edges, result.max_order)
    assert result.min_order.index(target) + 1 == result.min_rank
    assert result.max_order.index(target) + 1 == result.max_rank
    assert result.ancestors == {n for n in nodes if _path(n, target, edges)}
    assert result.descendants == {n for n in nodes if _path(target, n, edges)}


def _path(start: int, end: int, edges: list[tuple[int, int]]) -> bool:
    if start == end:
        return False
    seen = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == end:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(b for a, b in edges if a == node)
    return False


def test_cycle_detection_returns_closing_walk():
    edges = [(1, 2), (2, 3), (3, 1)]
    cycle = find_cycle([1, 2, 3], {n: {b for a, b in edges if a == n} for n in [1, 2, 3]})
    assert cycle is not None
    assert len(cycle) == 3
    assert topological_order([1, 2, 3], {n: {b for a, b in edges if a == n} for n in [1, 2, 3]}) is None
    with pytest.raises(CyclicGraphError):
        compute_rank([1, 2, 3], edges, 1)


def test_self_loop_is_reported_as_single_node_cycle():
    adjacency = {1: {1}}
    assert find_cycle([1], adjacency) == [1]


def test_deterministic_orders_for_isolated_nodes():
    result = compute_rank([3, 1, 2], [], 2)
    assert result.min_order == [2, 1, 3]
    assert result.max_order == [1, 3, 2]


def test_large_dag_is_fast_and_correct():
    # A 2000-node chain plus one independent layer: the target is pinned.
    count = 2001
    nodes = list(range(count))
    edges = [(index, index + 1) for index in range(count - 1)]
    result = compute_rank(nodes, edges, 1000)
    assert result.min_rank == result.max_rank == 1001
    assert len(result.min_order) == count
