"""Dependency edges between steps, cycle detection and ordering.

Dependencies only join siblings: a reference from inside a block to a step
outside it is a dependency of the enclosing block. A group of steps that
depend on each other is one cycle error; a reference to a later step that is
not part of a cycle is one later-step error. Errors on the references inside
a cycle are not repeated.
"""

import heapq
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from glow.validate.errors import Code, GlowError
from glow.validate.scopes import FieldPath, SymbolTable


@dataclass(frozen=True, slots=True)
class Graph:
    """`order` holds each step list in dependency order, keyed by owner (None is top level).

    `depends_on` maps a step to the siblings it uses. `bad_sites` are the
    expression sites whose references take part in a cycle or point forward.
    """

    order: dict[str | None, list[str]]
    depends_on: dict[str, list[str]]
    bad_sites: frozenset[tuple[str, FieldPath]]
    errors: list[GlowError]


def analyze(table: SymbolTable) -> Graph:
    position = {step_id: index for ids in table.lists.values() for index, step_id in enumerate(ids)}
    depends_on: dict[str, list[str]] = {}
    for dependency in table.dependencies:
        producers = depends_on.setdefault(dependency.consumer, [])
        if dependency.producer not in producers:
            producers.append(dependency.producer)

    errors: list[GlowError] = []
    component_of: dict[str, int] = {}
    nodes = [step_id for ids in table.lists.values() for step_id in ids]
    for number, component in enumerate(strongly_connected(nodes, depends_on)):
        node = component[0]
        if len(component) > 1 or node in depends_on.get(node, []):
            members = sorted(component, key=position.__getitem__)
            component_of.update(dict.fromkeys(members, number))
            errors.append(_cycle_error(members))

    bad_sites: set[tuple[str, FieldPath]] = set()
    for dependency in table.dependencies:
        consumer, producer = dependency.consumer, dependency.producer
        cyclic = consumer in component_of and component_of.get(producer) == component_of[consumer]
        if cyclic:
            bad_sites.add(dependency.site.key)
        elif position[producer] > position[consumer]:
            bad_sites.add(dependency.site.key)
            errors.append(_later_step_error(dependency.site.location, consumer, producer))

    order = {owner: _order(ids, depends_on) for owner, ids in table.lists.items()}
    return Graph(order, depends_on, frozenset(bad_sites), errors)


def _cycle_error(members: list[str]) -> GlowError:
    if len(members) == 1:
        message = f"step '{members[0]}' uses its own outputs"
    else:
        message = f"steps {', '.join(members)} use each other's outputs"
    return GlowError(
        Code.CYCLE,
        members[0],
        message,
        hint="remove one of the references so the steps can run in order",
    )


def _later_step_error(location: str, consumer: str, producer: str) -> GlowError:
    return GlowError(
        Code.LATER_STEP,
        location,
        f"steps.{producer} is declared after '{consumer}'; a step can only use earlier steps",
        hint=f"move '{producer}' above '{consumer}'",
    )


def strongly_connected(nodes: Iterable[str], edges: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative so deep chains do not hit the recursion limit."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)

    for root in nodes:
        if root in index:
            continue
        visit(root)
        work = [(root, iter(edges.get(root, ())))]
        while work:
            node, children = work[-1]
            for child in children:
                if child not in index:
                    visit(child)
                    work.append((child, iter(edges.get(child, ()))))
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    components.append(component)
    return components


def _order(ids: list[str], depends_on: Mapping[str, list[str]]) -> list[str]:
    """Topological order of one step list, keeping declaration order where free.

    Steps left over by a cycle follow in declaration order; a workflow with a
    cycle has already failed validation.
    """
    position = {step_id: index for index, step_id in enumerate(ids)}
    waiting = {
        step_id: {p for p in depends_on.get(step_id, []) if p in position and p != step_id}
        for step_id in ids
    }
    dependents: dict[str, list[str]] = {step_id: [] for step_id in ids}
    for step_id, producers in waiting.items():
        for producer in producers:
            dependents[producer].append(step_id)
    ready = [position[step_id] for step_id, producers in waiting.items() if not producers]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        step_id = ids[heapq.heappop(ready)]
        order.append(step_id)
        for dependent in dependents[step_id]:
            waiting[dependent].discard(step_id)
            if not waiting[dependent]:
                heapq.heappush(ready, position[dependent])
    placed = set(order)
    return order + [step_id for step_id in ids if step_id not in placed]
