"""Asignación conjunta de claves foráneas para una tabla.

La selección de una FK no es independiente cuando varias relaciones escriben
una misma columna local. Este módulo mantiene esa decisión en el nivel de tabla:
construye componentes de relaciones, coordina sus discriminadores y produce los
pares de una tabla puente sin una fase posterior que pueda romper cuotas o RI.

El módulo es privado deliberadamente. La IR, los planes y la API de generación
no dependen de sus clases.
"""

from __future__ import annotations

import bisect
import heapq
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from random import Random
from typing import Any, Protocol

from synthdb.generation.fk import (
    QuotaInfeasibleError,
    ZipfSelector,
    build_quota_assignment,
)
from synthdb.generation.seeding import seed_for_table
from synthdb.ir.schema import RelationshipSpec

FkKey = tuple[str, ...]
RandomState = tuple[Any, ...]
ErrorFactory = Callable[[str], BaseException]


class _SelectionWork(Protocol):
    """Parte de la instrumentación privada que usa el motor en los tests."""

    filter_scans: int
    bridge_pairs_examined: int
    bridge_quota_work: int
    compound_pairs_examined: int
    global_solver_work: int
    global_solver_states: int


@dataclass
class AssignmentRelation:
    """Entrada de una relación para el asignador de tabla."""

    fk: RelationshipSpec
    parent_rows: list[dict[str, Any]]
    strategy: str
    params: dict[str, Any]
    nullable_columns: tuple[str, ...]
    unresolved: bool = False
    allow_missing_parents: bool = False

    @property
    def key(self) -> FkKey:
        """Devuelve la clave estable de la FK."""
        return tuple(self.fk.columns)


@dataclass(frozen=True)
class CompoundUniqueConstraint:
    """Contrato privado de una restricción cubierta por varias FKs.

    ``relations`` contiene todas las FKs compatibles con la restricción;
    ``dimensions`` contiene solo las que aportan columnas propias a su
    proyección y, por tanto, multiplican su capacidad.
    """

    columns: tuple[str, ...]
    relations: tuple[FkKey, ...]
    dimensions: tuple[FkKey, ...] = ()


@dataclass
class _RelationState:
    relation: AssignmentRelation
    all_indices: tuple[int, ...]
    null_rows: list[bool] = field(default_factory=list)
    quota: list[int] | None = None
    unique_remaining: set[int] | None = None
    zipf_selectors: dict[tuple[int, ...], ZipfSelector] = field(default_factory=dict)
    groups: dict[tuple[Any, ...], list[int]] = field(default_factory=dict)

    @property
    def key(self) -> FkKey:
        """Devuelve la clave estable de la relación."""
        return self.relation.key


@dataclass
class _Component:
    """Componente conexa de FKs que comparten columnas locales."""

    states: list[_RelationState]
    discriminator: tuple[str, ...] = ()
    group_labels: list[tuple[Any, ...] | None] | None = None
    group_cache: dict[tuple[FkKey, ...], tuple[tuple[Any, ...], ...]] = field(default_factory=dict)
    parent_cache: dict[tuple[FkKey, tuple[Any, ...]], tuple[int, ...]] = field(default_factory=dict)


class TableAssigner:
    """Produce todas las asignaciones FK de una tabla de forma conjunta."""

    def __init__(
        self,
        *,
        table_name: str,
        table_kind: str,
        relations: list[AssignmentRelation],
        total: int,
        row_rngs: list[Random],
        seed: int,
        deferred_relation: FkKey = (),
        error_factory: ErrorFactory,
        work: _SelectionWork,
        deferred_null_columns: frozenset[str] = frozenset(),
        compound_unique_constraints: tuple[CompoundUniqueConstraint, ...] = (),
    ) -> None:
        self.table_name = table_name
        self.table_kind = table_kind
        self.relations = relations
        self.total = total
        self.row_rngs = row_rngs
        self.seed = seed
        self.deferred_relation = deferred_relation
        self._error = error_factory
        self.work = work
        self.deferred_null_columns = deferred_null_columns
        self.compound_unique_constraints = compound_unique_constraints
        self.states = {
            relation.key: _RelationState(
                relation=relation,
                all_indices=tuple(range(len(relation.parent_rows))),
                unique_remaining=(
                    set(range(len(relation.parent_rows)))
                    if relation.strategy == "unique_subset"
                    else None
                ),
            )
            for relation in relations
        }
        self.components = self._build_components()
        self.local_values_by_row: list[dict[str, Any]] = [{} for _ in range(total)]
        self._prepared = False
        self._row_assignments: list[dict[FkKey, int | None]] | None = None

    def assign_all(self) -> tuple[list[dict[FkKey, int | None]], list[RandomState]]:
        """Prepara y devuelve una asignación completa, estable por fila."""
        if self._row_assignments is not None:
            return self._row_assignments, [rng.getstate() for rng in self.row_rngs]

        self._prepare(validate_only=False)
        if self.compound_unique_constraints:
            assignments = self._assign_compound_unique_constraints()
        elif self.table_kind == "bridge" and len(self.relations) >= 2:
            assignments = self._assign_bridge()
        else:
            assignments = [{} for _ in range(self.total)]
            for row_index, rng in enumerate(self.row_rngs):
                assignments[row_index] = self.assign_row(row_index, rng)
        if self.compound_unique_constraints:
            for row_index, rng in enumerate(self.row_rngs):
                assignments[row_index].update(
                    self._assign_row_with_fixed(row_index, rng, assignments[row_index])
                )
        self._row_assignments = assignments
        return assignments, [rng.getstate() for rng in self.row_rngs]

    def prepare_for_rows(self) -> None:
        """Prepara estados para una tabla cuya fila recibe valores iniciales."""
        self._prepare(validate_only=True)

    def assign_row(
        self,
        row_index: int,
        rng: Random,
        initial_values: dict[str, Any] | None = None,
    ) -> dict[FkKey, int | None]:
        """Asigna las FKs de una fila usando valores locales ya fijados."""
        if not self._prepared:
            self._prepare(validate_only=True)
        if self.table_kind == "bridge" and len(self.relations) >= 2:
            raise self._error(
                f"tabla puente {self.table_name}: la asignación conjunta de un puente "
                "no admite generación fila a fila. Usa la fase normal de inserción."
            )
        if self.compound_unique_constraints:
            raise self._error(
                f"tabla {self.table_name}: las restricciones compuestas cubiertas por "
                "varias FKs requieren asignación por tabla completa; no se pueden "
                "resolver en una generación fila a fila."
            )

        values = initial_values or {}
        assignments: dict[FkKey, int | None] = {}
        locked = bool(self.deferred_null_columns) and all(
            values.get(column) is None for column in self.deferred_null_columns
        )
        for component in self.components:
            assignments.update(
                self._assign_component_row(component, row_index, rng, values, locked=locked)
            )
        assigned_keys = {state.key for component in self.components for state in component.states}
        for relation in self.relations:
            if relation.key not in assigned_keys:
                raise AssertionError(f"relación no asignada: {relation.key}")
        if self.deferred_relation:
            assignments[self.deferred_relation] = None
        return assignments

    def _prepare(self, *, validate_only: bool) -> None:
        if self._prepared:
            return
        self._validate_topology()
        if (
            self.table_kind == "bridge"
            and len(self.relations) >= 2
            and not self.compound_unique_constraints
        ):
            self._prepared = True
            return

        self._prepare_null_masks()
        compound_relation_keys = self._compound_relation_keys()
        for component in self.components:
            self._prepare_component(
                component,
                validate_only=validate_only,
                managed_excluded=compound_relation_keys,
            )
        self._prepared = True

    def _validate_topology(self) -> None:
        """Rechaza topologías conectadas sin discriminador universal antes de RNG."""
        for component in self.components:
            if len(component.states) < 2:
                continue
            common = set(component.states[0].relation.fk.columns)
            for state in component.states[1:]:
                common.intersection_update(state.relation.fk.columns)
            if not common:
                labels = ", ".join(
                    f"({', '.join(state.relation.fk.columns)}) -> {state.relation.fk.ref_table}"
                    for state in component.states
                )
                raise self._error(
                    f"tabla {self.table_name}: la componente conectada de FKs ({labels}) "
                    "no tiene un discriminador común universal. La topología no puede "
                    "asignarse conjuntamente de forma segura; separa las relaciones o "
                    "añade una columna discriminadora común."
                )

    def _prepare_null_masks(self) -> None:
        """Decide NULL por relación, sin compartir tiradas entre FKs."""
        for state in self.states.values():
            ratio = float(state.relation.params.get("null_ratio", 0.0))
            if state.relation.unresolved:
                state.null_rows = [True] * self.total
            elif state.relation.strategy == "quota" or ratio > 0.0:
                # quota historically consumed one row-RNG draw even at
                # null_ratio=0; preserve that stream so unrelated golden
                # fixtures remain byte-identical after the refactor.
                state.null_rows = [rng.random() < ratio for rng in self.row_rngs]
            else:
                state.null_rows = [False] * self.total

    def _prepare_component(
        self,
        component: _Component,
        *,
        validate_only: bool,
        managed_excluded: set[FkKey] | frozenset[FkKey] = frozenset(),
    ) -> None:
        if len(component.states) == 1:
            if component.states[0].key not in managed_excluded:
                self._prepare_single_quota(component.states[0])
            return

        common = set(component.states[0].relation.fk.columns)
        for state in component.states[1:]:
            common.intersection_update(state.relation.fk.columns)
        component.discriminator = tuple(sorted(common))
        for state in component.states:
            state.groups = _group_parent_indices(state.relation, component.discriminator)

        managed_states = [
            state
            for state in component.states
            if state.key not in managed_excluded
            and state.relation.strategy in {"quota", "unique_subset"}
        ]
        if managed_states and not validate_only:
            self._prepare_managed_component(component, managed_excluded)
        elif any(state.relation.strategy == "quota" for state in managed_states):
            # A leveled table supplies a discriminator per row. Its external FK
            # quotas are deliberately rejected before they can be silently
            # reassigned against that row-specific value.
            raise self._error(
                f"tabla {self.table_name}: una componente compartida con cuota no puede "
                "asignarse fila a fila junto a una autorreferencia diferida. Mueve la cuota "
                "a una fase normal o elimina la columna compartida de la autorreferencia."
            )

    def _prepare_single_quota(self, state: _RelationState) -> None:
        if state.relation.strategy != "quota":
            return
        if state.relation.unresolved:
            state.quota = [-1] * self.total
            return
        non_null = [index for index, is_null in enumerate(state.null_rows) if not is_null]
        state.quota = self._quota_for_rows(state, non_null)

    def _prepare_managed_component(
        self, component: _Component, managed_excluded: set[FkKey] | frozenset[FkKey]
    ) -> None:
        """Assign shared groups globally for quota and without-replacement FKs."""
        constrained = [
            state
            for state in component.states
            if state.key not in managed_excluded and _is_group_constraint_state(state)
        ]
        active_sets = {
            tuple(
                row_index for row_index in range(self.total) if self._state_active(state, row_index)
            )
            for state in constrained
            if any(self._state_active(state, row_index) for row_index in range(self.total))
            or (state.relation.strategy == "quota" and int(state.relation.params["min"]) > 0)
        }
        if len(active_sets) <= 1:
            self._prepare_fixed_group_component(component, constrained, active_sets)
        else:
            self._prepare_variable_group_component(component, constrained)

    def _prepare_fixed_group_component(
        self,
        component: _Component,
        constrained: list[_RelationState],
        active_sets: set[tuple[int, ...]],
    ) -> None:
        """Use exact group intervals when all active constraints share their rows."""
        active_rows = list(next(iter(active_sets), ()))
        active_states = [
            state
            for state in constrained
            if any(self._state_active(state, row_index) for row_index in active_rows)
        ]
        if not active_states:
            if any(
                state.relation.strategy == "quota" and int(state.relation.params["min"]) > 0
                for state in constrained
            ):
                raise self._global_component_error(component, constrained)
            component.group_labels = [None] * self.total
            for state in self.states.values():
                if state.relation.strategy == "quota":
                    state.quota = [-1] * self.total
            return

        common_groups = self._candidate_groups(component, active_states, None)
        ranges: dict[tuple[Any, ...], tuple[int, int]] = {}
        for group in common_groups:
            low = 0
            high = len(active_rows)
            valid = True
            for state in active_states:
                parents = state.groups.get(group, [])
                if not parents:
                    valid = False
                    break
                if state.relation.strategy == "quota":
                    minimum = int(state.relation.params["min"])
                    maximum = int(state.relation.params["max"])
                    low = max(low, len(parents) * minimum)
                    high = min(high, len(parents) * maximum)
                elif state.relation.strategy == "unique_subset":
                    # A group can consume each of its free unique parents at most once.
                    high = min(high, len(parents))
            if valid and low <= high:
                ranges[group] = (low, high)

        quota_states = [state for state in active_states if state.relation.strategy == "quota"]
        if quota_states:
            for state in quota_states:
                if int(state.relation.params["min"]) <= 0:
                    continue
                missing = [group for group in state.groups if group not in ranges]
                if missing:
                    raise self._global_component_error(component, constrained, missing[0])

        needed = len(active_rows)
        if not ranges:
            if needed == 0:
                component.group_labels = [None] * self.total
                for state in quota_states:
                    state.quota = [-1] * self.total
                return
            raise self._global_component_error(component, constrained)

        low_total = sum(low for low, _ in ranges.values())
        high_total = sum(high for _, high in ranges.values())
        if not low_total <= needed <= high_total:
            raise self._global_component_error(component, constrained)

        order = _stable_group_order(ranges)
        group_rng = Random(seed_for_table(self.seed, f"{self.table_name}:component-groups"))
        counts = _distribute_counts(order, ranges, needed, group_rng)
        labels: list[tuple[Any, ...]] = []
        for group in order:
            labels.extend([group] * counts[group])
        group_rng.shuffle(labels)
        component.group_labels = [None] * self.total
        for row_index, group in zip(active_rows, labels, strict=True):
            component.group_labels[row_index] = group
        self._fill_group_quota_assignments(component, quota_states)

    def _prepare_variable_group_component(
        self, component: _Component, constrained: list[_RelationState]
    ) -> None:
        """Find labels with an iterative DP over equivalent row signatures.

        A signature is the pair ``(active relations, candidate groups)``. All
        rows with the same signature contribute identically to every quota or
        ``unique_subset`` capacity, so the DP assigns their aggregate counts
        instead of keeping one state per row. The explicit stack makes search
        depth depend on the number of signatures, never on ``self.total``.
        """
        signature_rows: dict[tuple[tuple[FkKey, ...], tuple[tuple[Any, ...], ...]], list[int]] = {}
        for row_index in range(self.total):
            self.work.global_solver_work += 1
            active = [state for state in component.states if self._state_active(state, row_index)]
            constraining = [state for state in active if _is_group_constraint_state(state)]
            relevant = constraining or active
            options = self._candidate_groups(component, relevant, None) if relevant else ()
            if active and not options:
                raise self._incompatible_row_error(component, row_index)
            if options:
                active_keys = tuple(state.key for state in active)
                signature_rows.setdefault((active_keys, options), []).append(row_index)

        limited_states = [
            state for state in constrained if state.relation.strategy in {"quota", "unique_subset"}
        ]
        layout: list[tuple[_RelationState, tuple[Any, ...], int, int]] = []
        for state in limited_states:
            for group in _stable_group_order(state.groups):
                if state.relation.strategy == "quota":
                    minimum = len(state.groups[group]) * int(state.relation.params["min"])
                    maximum = len(state.groups[group]) * int(state.relation.params["max"])
                else:
                    minimum = 0
                    maximum = len(state.groups[group])
                layout.append((state, group, minimum, maximum))

        signatures = [
            (active_keys, options, rows) for (active_keys, options), rows in signature_rows.items()
        ]
        self.work.global_solver_work += sum(len(options) for _, options, _ in signatures)
        self.work.global_solver_states += len(signatures)
        signature_options: list[list[tuple[Any, ...]]] = []
        signature_increments: list[tuple[tuple[int, ...], ...]] = []
        for active_keys, options, _ in signatures:
            active_set = set(active_keys)
            ordered_options = list(options)
            signature_options.append(ordered_options)
            signature_increments.append(
                tuple(
                    tuple(
                        offset
                        for offset, (state, state_group, _, _) in enumerate(layout)
                        if state.key in active_set and state_group == group
                    )
                    for group in ordered_options
                )
            )

        future_possible = [[0] * len(layout) for _ in range(len(signatures) + 1)]
        for signature_index in range(len(signatures) - 1, -1, -1):
            future_possible[signature_index] = future_possible[signature_index + 1].copy()
            row_count = len(signatures[signature_index][2])
            for offsets in signature_increments[signature_index]:
                for offset in offsets:
                    future_possible[signature_index][offset] += row_count
                    self.work.global_solver_work += 1

        def allocation_candidates(
            row_count: int,
            increments: tuple[tuple[int, ...], ...],
            base_counts: tuple[int, ...],
            future_capacity: list[int],
        ) -> Iterator[tuple[int, ...]]:
            """Yield only distributions that can still satisfy residual bounds.

            The old generator enumerated every weak composition of ``row_count``
            and let ``apply_allocation`` reject almost all of them afterwards.
            Here every option receives a residual capacity before the iterative
            search starts, and each prefix is rejected when its remaining rows
            cannot meet a minimum with the current suffix and future signatures.
            The search remains complete, but its first candidate for bounded
            groups is already capacity-aware instead of concentrated in one
            group.
            """
            option_count = len(increments)
            option_caps: list[int] = []
            for offsets in increments:
                capacity = row_count
                for offset in offsets:
                    capacity = min(capacity, layout[offset][3] - base_counts[offset])
                    self.work.global_solver_work += 1
                option_caps.append(max(0, capacity))

            suffix_rows = [0] * (option_count + 1)
            for option_index in range(option_count - 1, -1, -1):
                suffix_rows[option_index] = (
                    suffix_rows[option_index + 1] + option_caps[option_index]
                )
                self.work.global_solver_work += 1
            if row_count > suffix_rows[0]:
                return

            suffix_resources = [0] * len(layout)
            for offsets, capacity in zip(increments, option_caps, strict=True):
                for offset in offsets:
                    suffix_resources[offset] += capacity
                    self.work.global_solver_work += 1

            def prefix_can_complete(
                next_index: int,
                remaining_rows: int,
                counts: list[int],
                changed_offsets: Sequence[int],
            ) -> bool:
                if remaining_rows < 0 or remaining_rows > suffix_rows[next_index]:
                    return False
                for offset in changed_offsets:
                    self.work.global_solver_work += 1
                    _, _, minimum, maximum = layout[offset]
                    count = counts[offset]
                    if (
                        count > maximum
                        or count + suffix_resources[offset] + future_capacity[offset] < minimum
                    ):
                        return False
                return True

            parts = [0] * option_count
            next_values: list[int | None] = [None] * option_count
            lower_bounds = [0] * option_count
            counts = list(base_counts)
            remaining_rows = row_count
            if not prefix_can_complete(0, remaining_rows, counts, range(len(layout))):
                return
            option_index = 0
            while option_index >= 0:
                if option_index == option_count:
                    if remaining_rows == 0:
                        yield tuple(parts)
                    option_index -= 1
                    if option_index >= 0:
                        amount = parts[option_index]
                        remaining_rows += amount
                        for offset in increments[option_index]:
                            counts[offset] -= amount
                            suffix_resources[offset] += option_caps[option_index]
                        parts[option_index] = 0
                    continue

                if next_values[option_index] is None:
                    capacity = option_caps[option_index]
                    high = remaining_rows
                    for offset in increments[option_index]:
                        high = min(high, layout[offset][3] - counts[offset])
                    later_resources = {
                        offset: suffix_resources[offset] - capacity
                        for offset in increments[option_index]
                    }
                    low = max(0, remaining_rows - suffix_rows[option_index + 1])
                    for offset in increments[option_index]:
                        low = max(
                            low,
                            layout[offset][2]
                            - counts[offset]
                            - later_resources[offset]
                            - future_capacity[offset],
                        )
                    lower_bounds[option_index] = low
                    next_values[option_index] = high

                candidate = next_values[option_index]
                assert candidate is not None
                next_values[option_index] = candidate - 1
                if candidate < lower_bounds[option_index]:
                    next_values[option_index] = None
                    if option_index == 0:
                        option_index = -1
                    else:
                        option_index -= 1
                        previous = parts[option_index]
                        remaining_rows += previous
                        for offset in increments[option_index]:
                            counts[offset] -= previous
                            suffix_resources[offset] += option_caps[option_index]
                        parts[option_index] = 0
                    continue

                parts[option_index] = candidate
                remaining_rows -= candidate
                for offset in increments[option_index]:
                    counts[offset] += candidate
                    suffix_resources[offset] -= option_caps[option_index]
                if not prefix_can_complete(
                    option_index + 1,
                    remaining_rows,
                    counts,
                    increments[option_index],
                ):
                    for offset in increments[option_index]:
                        counts[offset] -= candidate
                        suffix_resources[offset] += option_caps[option_index]
                    remaining_rows += candidate
                    parts[option_index] = 0
                    continue

                option_index += 1
                if option_index < option_count:
                    next_values[option_index] = None

        def apply_allocation(
            counts: tuple[int, ...],
            increments: tuple[tuple[int, ...], ...],
            allocation: tuple[int, ...],
        ) -> tuple[int, ...] | None:
            updated = list(counts)
            for amount, offsets in zip(allocation, increments, strict=True):
                if amount == 0:
                    continue
                self.work.global_solver_work += amount * len(offsets)
                for offset in offsets:
                    updated[offset] += amount
                    if updated[offset] > layout[offset][3]:
                        return None
            return tuple(updated)

        choice_rng = Random(seed_for_table(self.seed, f"{self.table_name}:component-ties"))
        for signature_index, shuffled_options in enumerate(signature_options):
            paired = list(zip(shuffled_options, signature_increments[signature_index], strict=True))
            choice_rng.shuffle(paired)
            signature_options[signature_index] = [option for option, _ in paired]
            signature_increments[signature_index] = tuple(increments for _, increments in paired)

        depth = 0
        initial_counts = (0,) * len(layout)
        prefix_counts = [initial_counts] * (len(signatures) + 1)
        iterators: list[Iterator[tuple[int, ...]] | None] = [None] * len(signatures)
        chosen: list[tuple[int, ...] | None] = [None] * len(signatures)
        while 0 <= depth < len(signatures):
            iterator = iterators[depth]
            if iterator is None:
                row_count = len(signatures[depth][2])
                iterator = allocation_candidates(
                    row_count,
                    signature_increments[depth],
                    prefix_counts[depth],
                    future_possible[depth + 1],
                )
                iterators[depth] = iterator
            base_counts = prefix_counts[depth]
            increments = signature_increments[depth]
            try:
                allocation = next(iterator)
            except StopIteration:
                iterators[depth] = None
                if depth == 0:
                    depth = -1
                else:
                    depth -= 1
                continue
            self.work.global_solver_states += 1
            self.work.global_solver_work += len(layout)
            updated = apply_allocation(base_counts, increments, allocation)
            if updated is None:
                continue
            if any(
                updated[offset] + future_possible[depth + 1][offset] < minimum
                for offset, (_, _, minimum, _) in enumerate(layout)
            ):
                continue
            chosen[depth] = allocation
            prefix_counts[depth + 1] = updated
            depth += 1

        if depth != len(signatures):
            raise self._global_component_error(component, constrained)

        labels: list[tuple[Any, ...] | None] = [None] * self.total
        for signature_index, (_, _, rows) in enumerate(signatures):
            signature_allocation = chosen[signature_index]
            if signature_allocation is None:
                raise AssertionError("la DP de firmas no conservó una solución")
            ordered_rows = list(rows)
            choice_rng.shuffle(ordered_rows)
            cursor = 0
            for group, amount in zip(
                signature_options[signature_index], signature_allocation, strict=True
            ):
                for row_index in ordered_rows[cursor : cursor + amount]:
                    labels[row_index] = group
                cursor += amount
            if cursor != len(rows):
                raise AssertionError("la DP de firmas no asignó todas las filas")
        component.group_labels = labels
        self._fill_group_quota_assignments(
            component,
            [state for state in component.states if state.relation.strategy == "quota"],
        )

    def _state_active(self, state: _RelationState, row_index: int) -> bool:
        """Return whether a relation must consume a parent on this row."""
        return (
            not state.relation.unresolved
            and not state.null_rows[row_index]
            and not (state.relation.allow_missing_parents and not state.relation.parent_rows)
        )

    def _global_component_error(
        self,
        component: _Component,
        states: Sequence[_RelationState],
        group: tuple[Any, ...] | None = None,
    ) -> BaseException:
        """Build one stable error for an impossible shared assignment."""
        detail = f" en el discriminador {group!r}" if group is not None else ""
        strategies = ", ".join(
            f"({', '.join(state.key)}) {state.relation.strategy}" for state in states
        )
        return self._error(
            f"tabla {self.table_name}: no existe una asignación global factible{detail} "
            f"para las relaciones {strategies} en {self.total} filas. Revisa las cuotas, "
            "la capacidad de unique_subset y los padres compatibles."
        )

    def _fill_group_quota_assignments(
        self, component: _Component, quota_states: list[_RelationState]
    ) -> None:
        labels = component.group_labels
        if labels is None:
            raise AssertionError("cuotas compartidas sin etiquetas de grupo")
        for state in quota_states:
            state.quota = [-1] * self.total
            groups = _stable_group_order(state.groups)
            for group in groups:
                rows = [
                    row_index
                    for row_index, label in enumerate(labels)
                    if label == group and not state.null_rows[row_index]
                ]
                if not rows:
                    if state.groups[group] and int(state.relation.params["min"]) > 0:
                        raise self._quota_component_error(component, quota_states, group)
                    continue
                positions = self._quota_for_group(state, group, len(rows))
                assert state.quota is not None
                for row_index, position in zip(rows, positions, strict=True):
                    state.quota[row_index] = state.groups[group][position]

    def _quota_for_group(
        self, state: _RelationState, group: tuple[Any, ...], n_rows: int
    ) -> list[int]:
        parents = state.groups.get(group, [])
        return self._quota_positions(state, len(parents), n_rows, parents_label=group)

    def _quota_for_rows(self, state: _RelationState, rows: list[int]) -> list[int]:
        positions = self._quota_positions(
            state, len(state.relation.parent_rows), len(rows), parents_label=None
        )
        quota = [-1] * self.total
        for row_index, position in zip(rows, positions, strict=True):
            quota[row_index] = position
        return quota

    def _quota_positions(
        self,
        state: _RelationState,
        n_parents: int,
        n_rows: int,
        *,
        parents_label: tuple[Any, ...] | None,
    ) -> list[int]:
        minimum = int(state.relation.params["min"])
        maximum = int(state.relation.params["max"])
        if n_parents == 0 and n_rows == 0 and minimum == 0:
            return []
        rng_name = f"{self.table_name}:{','.join(state.key)}"
        if parents_label is not None:
            rng_name += ":" + repr(parents_label)
        quota_rng = Random(seed_for_table(self.seed, rng_name))
        try:
            return build_quota_assignment(quota_rng, n_parents, n_rows, minimum, maximum)
        except QuotaInfeasibleError as exc:
            group_text = f" en el grupo {parents_label!r}" if parents_label is not None else ""
            raise self._error(
                f"tabla {self.table_name}, FK ({', '.join(state.key)}) -> "
                f"{state.relation.fk.ref_table} con cuota [min={minimum}, max={maximum}]"
                f"{group_text}: {exc}"
            ) from exc

    def _build_components(self) -> list[_Component]:
        remaining = list(self.states.values())
        components: list[_Component] = []
        while remaining:
            seed_state = remaining.pop(0)
            component_states = [seed_state]
            changed = True
            while changed:
                changed = False
                for state in remaining[:]:
                    if any(
                        set(state.relation.fk.columns).intersection(other.relation.fk.columns)
                        for other in component_states
                    ):
                        component_states.append(state)
                        remaining.remove(state)
                        changed = True
            components.append(_Component(component_states))
        return components

    def _assign_component_row(
        self,
        component: _Component,
        row_index: int,
        rng: Random,
        initial_values: dict[str, Any],
        *,
        locked: bool = False,
        fixed_assignments: Mapping[FkKey, int | None] | None = None,
    ) -> dict[FkKey, int | None]:
        fixed = fixed_assignments or {}
        if locked and any(
            self.deferred_null_columns.intersection(state.relation.fk.columns)
            for state in component.states
        ):
            return {
                state.key: fixed.get(state.key) if state.key in fixed else None
                for state in component.states
            }
        if len(component.states) == 1:
            state = component.states[0]
            if state.key in fixed:
                return {state.key: fixed[state.key]}
            return {state.key: self._assign_single_row(state, row_index, rng, initial_values)}

        active = [
            state
            for state in component.states
            if not state.relation.unresolved
            and not state.relation.allow_missing_parents
            and state.relation.parent_rows
            and not state.null_rows[row_index]
        ]
        label = component.group_labels[row_index] if component.group_labels is not None else None
        group = self._choose_group(component, active, row_index, rng, initial_values, label)
        assignments: dict[FkKey, int | None] = {}
        selected: int | None
        for state in component.states:
            if state.key in fixed:
                selected = fixed[state.key]
                if selected is not None:
                    candidates = self._candidate_indices(state, group, initial_values)
                    if selected not in candidates:
                        raise self._error(
                            f"tabla {self.table_name}, FK ({', '.join(state.key)}): "
                            f"la asignación conjunta fijó el padre índice {selected}, "
                            f"pero la fila {row_index} no es compatible con el "
                            "discriminador o las columnas locales compartidas."
                        )
                assignments[state.key] = selected
                continue
            if (
                state.relation.unresolved
                or (state.relation.allow_missing_parents and not state.relation.parent_rows)
                or state.null_rows[row_index]
            ):
                assignments[state.key] = None
                continue
            candidates = self._candidate_indices(state, group, initial_values)
            if state.relation.strategy == "quota":
                if state.quota is None:
                    raise self._error(
                        f"tabla {self.table_name}, FK ({', '.join(state.key)}): "
                        "la cuota no tiene una asignación conjunta preparada."
                    )
                selected = state.quota[row_index]
                if selected < 0:
                    selected = None
                elif selected not in candidates:
                    raise self._quota_conflict_error(state, selected, row_index)
            else:
                selected = self._pick_strategy(state, candidates, rng)
            if selected is None:
                if state.relation.nullable_columns:
                    assignments[state.key] = None
                    continue
                raise self._incompatible_row_error(component, row_index, state)
            assignments[state.key] = selected
        self._record_group_values(component, row_index, group, initial_values)
        return assignments

    def _assign_row_with_fixed(
        self,
        row_index: int,
        rng: Random,
        fixed_assignments: Mapping[FkKey, int | None],
    ) -> dict[FkKey, int | None]:
        """Completa una fila respetando FKs ya fijadas por una restricción."""
        initial_values = self.local_values_by_row[row_index]
        assignments: dict[FkKey, int | None] = {}
        locked = bool(self.deferred_null_columns) and all(
            initial_values.get(column) is None for column in self.deferred_null_columns
        )
        for component in self.components:
            assignments.update(
                self._assign_component_row(
                    component,
                    row_index,
                    rng,
                    initial_values,
                    locked=locked,
                    fixed_assignments=fixed_assignments,
                )
            )
        if self.deferred_relation:
            assignments[self.deferred_relation] = None
        return assignments

    def _assign_single_row(
        self,
        state: _RelationState,
        row_index: int,
        rng: Random,
        initial_values: dict[str, Any],
    ) -> int | None:
        if (
            state.relation.unresolved
            or state.relation.allow_missing_parents
            and not state.relation.parent_rows
            or state.null_rows[row_index]
        ):
            return None
        if state.relation.strategy == "quota":
            if state.quota is None:
                raise self._error(
                    f"tabla {self.table_name}, FK ({', '.join(state.key)}): cuota sin preparar."
                )
            selected = state.quota[row_index]
            if selected < 0:
                return None
            candidates = self._filter_initial(state, state.all_indices, initial_values)
            if selected not in candidates:
                raise self._quota_conflict_error(state, selected, row_index)
            return selected
        candidates = self._filter_initial(state, state.all_indices, initial_values)
        selected_index = self._pick_strategy(state, candidates, rng)
        if selected_index is None and not state.relation.nullable_columns:
            raise self._error(
                f"tabla {self.table_name}, FK ({', '.join(state.key)}) -> "
                f"{state.relation.fk.ref_table}: no hay padre compatible con los valores "
                f"locales fijados {initial_values!r}."
            )
        return selected_index

    def _choose_group(
        self,
        component: _Component,
        active: list[_RelationState],
        row_index: int,
        rng: Random,
        initial_values: dict[str, Any],
        label: tuple[Any, ...] | None,
    ) -> tuple[Any, ...] | None:
        group: tuple[Any, ...] | None
        fixed = _fixed_group(component.discriminator, initial_values)
        quota_groups = {
            _group_for_index(state, state.quota[row_index], component.discriminator)
            for state in active
            if state.relation.strategy == "quota"
            and state.quota is not None
            and state.quota[row_index] >= 0
        }
        if len(quota_groups) > 1:
            raise self._error(
                f"tabla {self.table_name}: dos cuotas fijan discriminadores incompatibles "
                f"en la fila {row_index}. Revisa las cuotas de las FKs compartidas."
            )
        forced = next(iter(quota_groups), None)
        if label is not None:
            if fixed is not None and fixed != label:
                raise self._error(
                    f"tabla {self.table_name}: la fila {row_index} ya fija el discriminador "
                    f"{fixed!r}, pero la cuota conjunta eligió {label!r}."
                )
            if forced is not None and forced != label:
                raise self._quota_conflict_group(component, row_index, label, forced)
            group = label
        elif forced is not None:
            group = forced
        elif fixed is not None:
            group = fixed
        else:
            constraining = [
                state
                for state in active
                if state.relation.strategy == "quota" or not state.relation.nullable_columns
            ]
            group_states = constraining or active
            group_keys = self._candidate_groups(component, group_states, None)
            unique_states = [
                state for state in active if state.relation.strategy == "unique_subset"
            ]
            if unique_states:
                group_keys = tuple(
                    group
                    for group in group_keys
                    if all(
                        self._candidate_indices(state, group, initial_values)
                        and (
                            state.unique_remaining is None
                            or any(
                                index in state.unique_remaining
                                for index in self._candidate_indices(state, group, initial_values)
                            )
                        )
                        for state in unique_states
                    )
                )
            if not group_keys:
                if active:
                    raise self._incompatible_row_error(component, row_index)
                return None
            if not active:
                group = group_keys[rng.randrange(len(group_keys))]
            else:
                first = next(
                    (state for state in active if state.relation.strategy == "unique_subset"),
                    active[0],
                )
                candidates = self._cached_parent_candidates(component, first, group_keys)
                if first.unique_remaining is not None:
                    candidates = tuple(
                        index for index in candidates if index in first.unique_remaining
                    )
                if not candidates:
                    raise self._incompatible_row_error(component, row_index, first)
                selected = candidates[rng.randrange(len(candidates))]
                group = _group_for_index(first, selected, component.discriminator)
        if group is None:
            return None
        for state in active:
            if state.relation.strategy != "quota" and state.relation.nullable_columns:
                continue
            if group not in state.groups:
                raise self._incompatible_row_error(component, row_index, state)
            if not self._candidate_indices(state, group, initial_values):
                raise self._incompatible_row_error(component, row_index, state)
        return group

    def _record_group_values(
        self,
        component: _Component,
        row_index: int,
        group: tuple[Any, ...] | None,
        initial_values: dict[str, Any],
    ) -> None:
        """Conserva discriminadores aunque todas las FKs de la fila sean NULL."""
        if group is None or not component.discriminator:
            return
        for state in component.states:
            indices = state.groups.get(group)
            if not indices:
                continue
            refs = dict(zip(state.relation.fk.columns, state.relation.fk.ref_columns, strict=True))
            parent = state.relation.parent_rows[indices[0]]
            for local in component.discriminator:
                if local not in initial_values:
                    self.local_values_by_row[row_index][local] = parent.get(refs[local])
            return

    def _candidate_groups(
        self,
        component: _Component,
        states: Sequence[_RelationState],
        fixed: tuple[Any, ...] | None,
    ) -> tuple[tuple[Any, ...], ...]:
        relevant = tuple(state.key for state in states if not state.relation.unresolved)
        if not relevant:
            relevant = tuple(state.key for state in component.states)
        cached = component.group_cache.get(relevant)
        if cached is None:
            group_sets = [set(self.states[key].groups) for key in relevant]
            common = set.intersection(*group_sets) if group_sets else set()
            cached = tuple(_stable_group_order({group: [] for group in common}))
            component.group_cache[relevant] = cached
        if fixed is None:
            return cached
        return tuple(group for group in cached if group == fixed)

    def _cached_parent_candidates(
        self,
        component: _Component,
        state: _RelationState,
        groups: tuple[tuple[Any, ...], ...],
    ) -> tuple[int, ...]:
        key = (state.key, tuple(groups))
        cached = component.parent_cache.get(key)
        if cached is not None:
            return cached
        result: list[int] = []
        group_set = set(groups)
        for index in state.all_indices:
            self.work.filter_scans += 1
            if _group_for_index(state, index, component.discriminator) in group_set:
                result.append(index)
        cached = tuple(result)
        component.parent_cache[key] = cached
        return cached

    def _candidate_indices(
        self,
        state: _RelationState,
        group: tuple[Any, ...] | None,
        initial_values: dict[str, Any],
    ) -> tuple[int, ...]:
        candidates = state.all_indices if group is None else tuple(state.groups.get(group, []))
        return self._filter_initial(state, candidates, initial_values)

    def _filter_initial(
        self,
        state: _RelationState,
        candidates: Sequence[int],
        initial_values: dict[str, Any],
    ) -> tuple[int, ...]:
        if not initial_values:
            return tuple(candidates)
        refs = dict(zip(state.relation.fk.columns, state.relation.fk.ref_columns, strict=True))
        result: list[int] = []
        for index in candidates:
            parent = state.relation.parent_rows[index]
            if all(
                local not in initial_values
                or initial_values[local] is None
                or initial_values[local] == parent.get(ref)
                for local, ref in refs.items()
            ):
                result.append(index)
        return tuple(result)

    def _pick_strategy(
        self, state: _RelationState, candidates: Sequence[int], rng: Random
    ) -> int | None:
        if not candidates:
            return None
        if state.relation.strategy == "unique_subset":
            if state.unique_remaining is None:
                raise AssertionError("unique_subset sin estado de padres")
            available = [index for index in candidates if index in state.unique_remaining]
            if not available:
                return None
            selected = available[rng.randrange(len(available))]
            state.unique_remaining.remove(selected)
            return selected
        if state.relation.strategy == "zipf":
            key = tuple(candidates)
            selector = state.zipf_selectors.get(key)
            if selector is None:
                selector = ZipfSelector(len(key), float(state.relation.params.get("s", 1.2)))
                state.zipf_selectors[key] = selector
            position = selector.pick(rng)
            if position is None:
                return None
            return key[position]
        return candidates[rng.randrange(len(candidates))]

    def _compound_relation_keys(self) -> set[FkKey]:
        """Return the FK keys participating in a compound-UNIQUE contract."""
        return {
            relation
            for constraint in self.compound_unique_constraints
            for relation in (constraint.dimensions or constraint.relations)
        }

    def _assign_compound_unique_constraints(
        self,
    ) -> list[dict[FkKey, int | None]]:
        """Build assignments for every contracted UNIQUE without replacement."""
        assignments: list[dict[FkKey, int | None]] = [{} for _ in range(self.total)]
        by_relations: dict[tuple[FkKey, ...], list[CompoundUniqueConstraint]] = {}
        for constraint in self.compound_unique_constraints:
            relation_keys = constraint.dimensions or constraint.relations
            if len(constraint.columns) < 2 or len(relation_keys) < 2:
                raise self._error(
                    f"tabla {self.table_name}: la restricción compuesta "
                    f"({', '.join(constraint.columns)}) no tiene al menos dos FKs "
                    "implicadas."
                )
            missing = [key for key in relation_keys if key not in self.states]
            if missing:
                raise self._error(
                    f"tabla {self.table_name}: la restricción compuesta "
                    f"({', '.join(constraint.columns)}) requiere FKs no disponibles "
                    f"({', '.join(', '.join(key) for key in missing)})."
                )
            by_relations.setdefault(relation_keys, []).append(constraint)

        relation_groups = list(by_relations)
        for index, left_group in enumerate(relation_groups):
            left_components = {
                component_index
                for component_index, component in enumerate(self.components)
                if any(state.key in left_group for state in component.states)
            }
            for right_group in relation_groups[index + 1 :]:
                right_components = {
                    component_index
                    for component_index, component in enumerate(self.components)
                    if any(state.key in right_group for state in component.states)
                }
                if left_components.intersection(right_components):
                    left_text = "; ".join(
                        f"({', '.join(item.columns)})" for item in by_relations[left_group]
                    )
                    right_text = "; ".join(
                        f"({', '.join(item.columns)})" for item in by_relations[right_group]
                    )
                    raise self._error(
                        f"tabla {self.table_name}: las restricciones compuestas "
                        f"{left_text} y {right_text} comparten una componente de FKs "
                        "y no tienen una asignación conjunta segura. Ajusta las "
                        "restricciones o la cardinalidad antes de generar."
                    )

        for relation_keys, constraints in by_relations.items():
            if len(relation_keys) != 2:
                self._assign_product_constraints(assignments, relation_keys, constraints)
            else:
                self._assign_pair_constraints(assignments, relation_keys, constraints)
        return assignments

    def _assign_product_constraints(
        self,
        assignments: list[dict[FkKey, int | None]],
        relation_keys: tuple[FkKey, ...],
        constraints: Sequence[CompoundUniqueConstraint],
    ) -> None:
        """Assign a uniform/unique-subset product for three or more FKs.

        The product is addressed by mixed-radix ranks, so the complete
        Cartesian edge set is never built. Quotas and mixed limited strategies
        remain on the binary Havel--Hakimi path; rejecting those combinations
        here is safer than silently weakening their contract.
        """
        states = [self.states[key] for key in relation_keys]
        if any(state.relation.strategy == "quota" for state in states):
            raise self._error(
                f"tabla {self.table_name}: las restricciones compuestas "
                f"({'; '.join(', '.join(item.columns) for item in constraints)}) "
                "con tres o más FKs no admiten quota todavía; ajusta la estrategia "
                "o separa la restricción antes de generar."
            )
        strategies = {state.relation.strategy for state in states}
        if "unique_subset" in strategies and strategies != {"unique_subset"}:
            raise self._error(
                f"tabla {self.table_name}: las restricciones compuestas "
                f"({'; '.join(', '.join(item.columns) for item in constraints)}) "
                "mezclan unique_subset con estrategias sin límite en tres o más "
                "FKs; no existe una asignación segura para ese contrato."
            )
        active_rows = [
            row_index
            for row_index in range(self.total)
            if all(
                not state.relation.unresolved
                and not state.null_rows[row_index]
                and not (state.relation.allow_missing_parents and not state.relation.parent_rows)
                and bool(state.relation.parent_rows)
                for state in states
            )
        ]
        if not active_rows:
            return

        common = set(states[0].relation.fk.columns)
        for state in states[1:]:
            common.intersection_update(state.relation.fk.columns)
        raw_groups: dict[tuple[Any, ...], tuple[list[int], ...]]
        if common:
            grouped = [
                _group_parent_indices(state.relation, tuple(sorted(common))) for state in states
            ]
            labels = set(grouped[0])
            for groups in grouped[1:]:
                labels.intersection_update(groups)
            raw_groups = {
                label: tuple(groups[label] for groups in grouped)
                for label in _stable_group_order({label: [] for label in labels})
            }
        else:
            raw_groups = {
                (): tuple(list(state.all_indices) for state in states),
            }

        options_by_group: dict[tuple[Any, ...], tuple[list[int], ...]] = {}
        for label, raw_options in raw_groups.items():
            options_by_group[label] = tuple(
                _distinct_parent_indices(
                    state,
                    indices,
                    constraints,
                    relation_keys.index(state.key),
                    relation_keys,
                )
                for state, indices in zip(states, raw_options, strict=True)
            )
        capacities = {
            label: (
                min(len(options) for options in options_by_group[label])
                if strategies == {"unique_subset"}
                else _product_size(options_by_group[label])
            )
            for label in options_by_group
        }
        capacity = sum(capacities.values())
        constraint_text = "; ".join(
            f"({', '.join(constraint.columns)})" for constraint in constraints
        )
        if len(active_rows) > capacity:
            raise self._error(
                f"tabla {self.table_name}: la restricción {constraint_text} solicita "
                f"{len(active_rows)} filas activas pero solo tiene "
                f"{_compound_capacity_text(capacity)} "
                "sin repetición. Reduce las filas "
                "solicitadas o añade padres compatibles."
            )
        pair_rng = Random(
            seed_for_table(
                self.seed,
                f"{self.table_name}:compound:"
                f"{','.join(','.join(item.columns) for item in constraints)}",
            )
        )
        if strategies == {"unique_subset"}:
            ranges = {label: (0, value) for label, value in capacities.items()}
            order = _stable_group_order(ranges)
            counts = _distribute_counts(order, ranges, len(active_rows), pair_rng)
            tuples: list[tuple[int, ...]] = []
            for label in order:
                options = [list(items) for items in options_by_group[label]]
                for items in options:
                    pair_rng.shuffle(items)
                tuples.extend(
                    tuple(items[position] for items in options) for position in range(counts[label])
                )
        else:
            tuples = _sample_uniform_tuples(options_by_group, len(active_rows), pair_rng)
        if len(tuples) != len(active_rows):
            raise self._error(
                f"tabla {self.table_name}: la restricción {constraint_text} solo "
                f"produjo {len(tuples)} de {len(active_rows)} asignaciones requeridas."
            )
        self.work.compound_pairs_examined += len(tuples)
        for row_index, selected in zip(active_rows, tuples, strict=True):
            for state, parent_index in zip(states, selected, strict=True):
                assignments[row_index][state.key] = parent_index
                if state.relation.strategy == "unique_subset":
                    if state.unique_remaining is None or parent_index not in state.unique_remaining:
                        raise self._error(
                            f"tabla {self.table_name}: la restricción {constraint_text} "
                            f"reutilizaría el padre índice {parent_index} de la FK "
                            f"({', '.join(state.key)}) pese a unique_subset."
                        )
                    state.unique_remaining.remove(parent_index)
                self._record_parent_values(row_index, state, parent_index)

    def _assign_pair_constraints(
        self,
        assignments: list[dict[FkKey, int | None]],
        relation_keys: tuple[FkKey, ...],
        constraints: Sequence[CompoundUniqueConstraint],
    ) -> None:
        """Assign one binary family of compound UNIQUE constraints."""
        first = self.states[relation_keys[0]]
        second = self.states[relation_keys[1]]
        active_rows = [
            row_index
            for row_index in range(self.total)
            if all(
                not state.relation.unresolved
                and not state.null_rows[row_index]
                and not (state.relation.allow_missing_parents and not state.relation.parent_rows)
                and bool(state.relation.parent_rows)
                for state in (first, second)
            )
        ]
        if not active_rows:
            return

        shared = [
            column for column in first.relation.fk.columns if column in second.relation.fk.columns
        ]
        groups = _projected_pair_groups(
            first,
            second,
            shared,
            constraints,
            relation_keys,
        )
        available = sum(len(lefts) * len(rights) for lefts, rights in groups.values())
        constraint_text = "; ".join(
            f"({', '.join(constraint.columns)})" for constraint in constraints
        )
        if len(active_rows) > available:
            raise self._error(
                f"tabla {self.table_name}: la restricción {constraint_text} solicita "
                f"{len(active_rows)} filas activas pero solo tiene "
                f"{_compound_capacity_text(available)} "
                "sin repetición. Reduce las filas "
                "solicitadas o añade padres compatibles."
            )

        left_limited = _is_degree_limited(first)
        right_limited = _is_degree_limited(second)
        pair_rng = Random(
            seed_for_table(
                self.seed,
                f"{self.table_name}:compound:"
                f"{','.join(','.join(item.columns) for item in constraints)}",
            )
        )
        try:
            if not left_limited and not right_limited:
                pairs = _sample_uniform_pairs(groups, len(active_rows), pair_rng)
            else:
                pairs = self._quota_bridge_pairs(
                    first,
                    second,
                    groups,
                    len(active_rows),
                    pair_rng,
                    left_limited,
                    right_limited,
                )
        except (QuotaInfeasibleError, RuntimeError, ValueError) as exc:
            table_label = (
                f"tabla puente {self.table_name}"
                if self.table_kind == "bridge"
                else f"tabla {self.table_name}"
            )
            detail = str(exc)
            if self.table_kind != "bridge":
                detail = detail.replace(f"tabla puente {self.table_name}", table_label, 1)
            raise self._error(
                f"{table_label}: la restricción {constraint_text} no tiene "
                f"una asignación conjunta factible para {len(active_rows)} filas: {detail}"
            ) from exc

        if len(pairs) != len(active_rows):
            raise self._error(
                f"tabla {self.table_name}: la restricción {constraint_text} solo "
                f"produjo {len(pairs)} de {len(active_rows)} asignaciones requeridas."
            )
        self.work.compound_pairs_examined += len(pairs)
        if self.table_kind == "bridge":
            self.work.bridge_pairs_examined += len(pairs)
        pair_by_row = list(zip(active_rows, pairs, strict=True))
        pair_by_row.sort(key=lambda item: item[0])
        for row_index, (left_index, right_index) in pair_by_row:
            assignments[row_index][first.key] = left_index
            assignments[row_index][second.key] = right_index
            for state, parent_index in ((first, left_index), (second, right_index)):
                if state.relation.strategy == "unique_subset":
                    if state.unique_remaining is None or parent_index not in state.unique_remaining:
                        raise self._error(
                            f"tabla {self.table_name}: la restricción {constraint_text} "
                            f"reutilizaría el padre índice {parent_index} de la FK "
                            f"({', '.join(state.key)}) pese a unique_subset."
                        )
                    state.unique_remaining.remove(parent_index)
            self._record_parent_values(row_index, first, left_index)
            self._record_parent_values(row_index, second, right_index)

    def _record_parent_values(
        self, row_index: int, state: _RelationState, parent_index: int
    ) -> None:
        """Expose contracted FK values to other relations in the same row."""
        refs = dict(zip(state.relation.fk.columns, state.relation.fk.ref_columns, strict=True))
        parent = state.relation.parent_rows[parent_index]
        for local, ref in refs.items():
            value = parent.get(ref)
            if value is None:
                continue
            existing = self.local_values_by_row[row_index].get(local)
            if existing is not None and existing != value:
                raise self._error(
                    f"tabla {self.table_name}: las restricciones compuestas fijan "
                    f"valores incompatibles para la columna compartida '{local}' "
                    f"en la fila {row_index}: {existing!r} y {value!r}."
                )
            self.local_values_by_row[row_index][local] = value

    def _assign_bridge(self) -> list[dict[FkKey, int | None]]:
        if len(self.relations) != 2:
            raise self._error(
                f"tabla puente {self.table_name}: se esperaban exactamente dos FKs para "
                "la asignación conjunta; separa las relaciones adicionales."
            )
        first, second = self.states.values()
        shared = [
            column for column in first.relation.fk.columns if column in second.relation.fk.columns
        ]
        groups = _bridge_groups(first, second, shared)
        available = sum(len(lefts) * len(rights) for lefts, rights in groups.values())
        if available < self.total:
            detail = (
                f"compatibles por las columnas compartidas ({', '.join(shared)})"
                if shared
                else "de FK distintas"
            )
            raise self._error(
                f"tabla puente {self.table_name}: se piden {self.total} filas pero solo "
                f"existen {available} combinaciones {detail} sin repetir. Reduce la "
                "cardinalidad o añade filas padre compatibles."
            )

        left_limited = _is_degree_limited(first)
        right_limited = _is_degree_limited(second)
        bridge_rng = Random(seed_for_table(self.seed, f"{self.table_name}:bridge"))
        try:
            if not left_limited and not right_limited:
                pairs = _sample_uniform_pairs(groups, self.total, bridge_rng)
            else:
                pairs = self._quota_bridge_pairs(
                    first, second, groups, self.total, bridge_rng, left_limited, right_limited
                )
        except (QuotaInfeasibleError, RuntimeError, ValueError) as exc:
            raise self._error(
                f"tabla puente {self.table_name}: la asignación conjunta es infactible: {exc}"
            ) from exc
        self.work.bridge_pairs_examined += len(pairs)
        first_key, second_key = first.key, second.key
        return [{first_key: left, second_key: right} for left, right in pairs]

    def _quota_bridge_pairs(
        self,
        first: _RelationState,
        second: _RelationState,
        groups: dict[tuple[Any, ...], tuple[list[int], list[int]]],
        total: int,
        rng: Random,
        left_limited: bool,
        right_limited: bool,
    ) -> list[tuple[int, int]]:
        ranges: dict[tuple[Any, ...], tuple[int, int]] = {}
        for group, (lefts, rights) in groups.items():
            capacity = len(lefts) * len(rights)
            intervals = [(0, capacity)]
            if left_limited:
                intervals.append(
                    _quota_group_interval(first, len(lefts), len(rights), self.table_name)
                )
            if right_limited:
                intervals.append(
                    _quota_group_interval(second, len(rights), len(lefts), self.table_name)
                )
            low = max(interval[0] for interval in intervals)
            high = min(interval[1] for interval in intervals)
            if low <= high:
                ranges[group] = (low, high)
            elif any(interval[0] > 0 for interval in intervals[1:]):
                raise self._error(
                    f"tabla puente {self.table_name}: las cuotas no tienen una asignación "
                    f"conjunta para el grupo {group!r}. Ajusta min/max o añade padres "
                    "compatibles."
                )
        if not ranges:
            raise self._error(
                f"tabla puente {self.table_name}: no hay grupos compatibles para satisfacer "
                "las cuotas configuradas."
            )
        low = sum(interval[0] for interval in ranges.values())
        high = sum(interval[1] for interval in ranges.values())
        if not low <= total <= high:
            raise self._error(
                f"tabla puente {self.table_name}: se piden {total} filas y las cuotas "
                f"solo permiten un total entre {low} y {high}. Ajusta min/max o la "
                "cardinalidad de la tabla."
            )
        order = _stable_group_order(ranges)
        counts = _distribute_counts(order, ranges, total, rng)
        pairs: list[tuple[int, int]] = []
        for group in order:
            lefts, rights = groups[group]
            pairs.extend(
                _build_group_pairs(
                    lefts,
                    rights,
                    counts[group],
                    first if left_limited else None,
                    second if right_limited else None,
                    rng,
                    self.table_name,
                    self.work,
                )
            )
        rng.shuffle(pairs)
        return pairs

    def _incompatible_row_error(
        self,
        component: _Component,
        row_index: int,
        state: _RelationState | None = None,
    ) -> BaseException:
        relation = (
            f", FK ({', '.join(state.key)}) -> {state.relation.fk.ref_table}"
            if state is not None
            else ""
        )
        return self._error(
            f"tabla {self.table_name}{relation}: la fila {row_index} no tiene una "
            "combinación de padres compatible con las columnas compartidas; no hay padre "
            "compatible. Revisa los padres disponibles, las FKs o la cardinalidad "
            "solicitada."
        )

    def _quota_component_error(
        self,
        component: _Component,
        quota_states: list[_RelationState],
        group: tuple[Any, ...],
    ) -> BaseException:
        return self._error(
            f"tabla {self.table_name}: las cuotas compartidas {_relations_label(quota_states)} "
            f"no tienen una asignación conjunta para el discriminador {group!r}. Añade "
            "padres compatibles, baja min o revisa las columnas compartidas."
        )

    def _quota_conflict_error(
        self, state: _RelationState, selected: int, row_index: int
    ) -> BaseException:
        return self._error(
            f"tabla {self.table_name}, FK ({', '.join(state.key)}) -> "
            f"{state.relation.fk.ref_table}: la cuota asignó el padre índice {selected}, "
            f"pero no es compatible con el discriminador de la fila {row_index}. La cuota "
            "no puede reasignarse en silencio; revisa las relaciones compartidas."
        )

    def _quota_conflict_group(
        self,
        component: _Component,
        row_index: int,
        label: tuple[Any, ...],
        forced: tuple[Any, ...],
    ) -> BaseException:
        return self._error(
            f"tabla {self.table_name}: la cuota conjunta eligió el discriminador {label!r} "
            f"pero la cuota de la fila {row_index} exige {forced!r}. Revisa las cuotas "
            "de las FKs compartidas."
        )


def _is_group_constraint_state(state: _RelationState) -> bool:
    """Return whether a relation constrains the shared group of a row."""
    return (
        state.relation.strategy in {"quota", "unique_subset"} or not state.relation.nullable_columns
    )


def _group_parent_indices(
    relation: AssignmentRelation, discriminator: Sequence[str]
) -> dict[tuple[Any, ...], list[int]]:
    refs = dict(zip(relation.fk.columns, relation.fk.ref_columns, strict=True))
    groups: dict[tuple[Any, ...], list[int]] = {}
    for index, row in enumerate(relation.parent_rows):
        group = tuple(_marker(row.get(refs[column])) for column in discriminator)
        groups.setdefault(group, []).append(index)
    return groups


def _group_for_index(
    state: _RelationState, index: int | None, discriminator: Sequence[str]
) -> tuple[Any, ...] | None:
    if index is None:
        return None
    refs = dict(zip(state.relation.fk.columns, state.relation.fk.ref_columns, strict=True))
    row = state.relation.parent_rows[index]
    return tuple(_marker(row.get(refs[column])) for column in discriminator)


def _fixed_group(discriminator: Sequence[str], values: dict[str, Any]) -> tuple[Any, ...] | None:
    if not discriminator or any(
        column not in values or values[column] is None for column in discriminator
    ):
        return None
    return tuple(_marker(values[column]) for column in discriminator)


def _relations_label(states: Sequence[_RelationState]) -> str:
    return "; ".join(
        f"({', '.join(state.key)}) -> {state.relation.fk.ref_table} "
        f"[min={state.relation.params.get('min')}, max={state.relation.params.get('max')}]"
        for state in states
    )


def _stable_group_order(groups: dict[tuple[Any, ...], Any]) -> list[tuple[Any, ...]]:
    """Ordena grupos sin depender de la comparación entre tipos de valores."""
    return sorted(
        groups,
        key=lambda group: tuple((type(value).__name__, repr(value)) for value in group),
    )


def _distribute_counts(
    order: Sequence[tuple[Any, ...]],
    ranges: dict[tuple[Any, ...], tuple[int, int]],
    needed: int,
    rng: Random,
) -> dict[tuple[Any, ...], int]:
    counts = {group: ranges[group][0] for group in order}
    remaining = needed - sum(counts.values())
    available = [group for group in order if ranges[group][1] > counts[group]]
    while remaining:
        if not available:
            raise ValueError("grupo sin capacidad durante la distribución")
        position = rng.randrange(len(available))
        group = available[position]
        counts[group] += 1
        remaining -= 1
        if counts[group] == ranges[group][1]:
            last = available.pop()
            if position < len(available):
                available[position] = last
    return counts


def _bridge_groups(
    first: _RelationState,
    second: _RelationState,
    shared: Sequence[str],
) -> dict[tuple[Any, ...], tuple[list[int], list[int]]]:
    if not shared:
        return {(): (list(first.all_indices), list(second.all_indices))}
    left_groups = _group_parent_indices(first.relation, shared)
    right_groups = _group_parent_indices(second.relation, shared)
    return {
        group: (lefts, right_groups[group])
        for group, lefts in left_groups.items()
        if group in right_groups
    }


def _projected_pair_groups(
    first: _RelationState,
    second: _RelationState,
    shared: Sequence[str],
    constraints: Sequence[CompoundUniqueConstraint],
    relation_keys: Sequence[FkKey],
) -> dict[tuple[Any, ...], tuple[list[int], list[int]]]:
    """Group binary parents and collapse duplicate UNIQUE projections."""
    raw_groups = _bridge_groups(first, second, shared)
    return {
        group: (
            _distinct_parent_indices(first, lefts, constraints, 0, relation_keys),
            _distinct_parent_indices(second, rights, constraints, 1, relation_keys),
        )
        for group, (lefts, rights) in raw_groups.items()
    }


def _sample_uniform_pairs(
    groups: dict[tuple[Any, ...], tuple[list[int], list[int]]],
    count: int,
    rng: Random,
) -> list[tuple[int, int]]:
    order = _stable_group_order(groups)
    prefixes: list[int] = []
    total = 0
    for group in order:
        lefts, rights = groups[group]
        total += len(lefts) * len(rights)
        prefixes.append(total)
    if count > total:
        raise ValueError("se solicitaron más pares que los disponibles")
    selected = rng.sample(range(total), count)
    pairs: list[tuple[int, int]] = []
    for flat in selected:
        group_index = bisect.bisect_right(prefixes, flat)
        previous = prefixes[group_index - 1] if group_index else 0
        lefts, rights = groups[order[group_index]]
        offset = flat - previous
        right_count = len(rights)
        left_index, right_index = divmod(offset, right_count)
        pairs.append((lefts[left_index], rights[right_index]))
    return pairs


def _distinct_parent_indices(
    state: _RelationState,
    indices: Sequence[int],
    constraints: Sequence[CompoundUniqueConstraint],
    relation_position: int,
    relation_keys: Sequence[FkKey],
) -> list[int]:
    """Keep one parent for each projected compound-UNIQUE contribution."""
    refs = dict(zip(state.relation.fk.columns, state.relation.fk.ref_columns, strict=True))
    projections: dict[tuple[tuple[int, str, Any], ...], int] = {}
    for index in indices:
        parent = state.relation.parent_rows[index]
        projection_parts: list[tuple[int, str, Any]] = []
        for constraint_index, constraint in enumerate(constraints):
            for column in constraint.columns:
                owner = next(
                    position
                    for position, relation_key in enumerate(relation_keys)
                    if column in relation_key
                )
                if owner == relation_position:
                    projection_parts.append(
                        (constraint_index, column, _marker(parent.get(refs[column])))
                    )
        projection = tuple(projection_parts)
        projections.setdefault(projection, index)
    return list(projections.values())


def _product_size(options: Sequence[Sequence[int]]) -> int:
    """Return a mixed-radix product without constructing its tuples."""
    result = 1
    for items in options:
        result *= len(items)
    return result


def _compound_capacity_text(capacity: int) -> str:
    """Describe a compound UNIQUE capacity with stable singular/plural wording."""
    if capacity == 1:
        return "capacidad 1 (1 combinación compatible)"
    return f"capacidad {capacity} ({capacity} combinaciones compatibles)"


def _sample_uniform_tuples(
    groups: dict[tuple[Any, ...], tuple[list[int], ...]],
    count: int,
    rng: Random,
) -> list[tuple[int, ...]]:
    """Sample distinct mixed-radix tuples without materializing the product."""
    order = _stable_group_order(groups)
    prefixes: list[int] = []
    total = 0
    for group in order:
        total += _product_size(groups[group])
        prefixes.append(total)
    if count > total:
        raise ValueError("se solicitaron más tuplas que las disponibles")
    selected = rng.sample(range(total), count)
    tuples: list[tuple[int, ...]] = []
    for flat in selected:
        group_index = bisect.bisect_right(prefixes, flat)
        previous = prefixes[group_index - 1] if group_index else 0
        options = groups[order[group_index]]
        offset = flat - previous
        values: list[int] = []
        for items in reversed(options):
            offset, position = divmod(offset, len(items))
            values.append(items[position])
        tuples.append(tuple(reversed(values)))
    return tuples


def _quota_group_interval(
    state: _RelationState,
    parent_count: int,
    opposite_count: int,
    table_name: str,
) -> tuple[int, int]:
    if getattr(state.relation, "strategy", "quota") == "unique_subset":
        return 0, parent_count
    minimum = int(state.relation.params["min"])
    maximum = int(state.relation.params["max"])
    if minimum > opposite_count:
        raise RuntimeError(
            f"tabla puente {table_name}: la FK ({', '.join(state.key)}) exige min={minimum} "
            f"pero cada padre compatible solo admite {opposite_count} pares únicos."
        )
    return parent_count * minimum, parent_count * min(maximum, opposite_count)


def _build_group_pairs(
    lefts: list[int],
    rights: list[int],
    count: int,
    left_quota: _RelationState | None,
    right_quota: _RelationState | None,
    rng: Random,
    table_name: str,
    work: _SelectionWork,
) -> list[tuple[int, int]]:
    """Construct a bounded simple b-matching without the complete edge graph."""
    left_min, left_max = _bridge_degree_bounds(left_quota, len(rights))
    right_min, right_max = _bridge_degree_bounds(right_quota, len(lefts))
    if count < 0:
        raise RuntimeError(f"tabla puente {table_name}: la cardinalidad solicitada es negativa.")
    if not lefts or not rights:
        if count == 0 and left_min == right_min == 0:
            work.bridge_quota_work += len(lefts) + len(rights)
            return []
        raise RuntimeError(
            f"tabla puente {table_name}: no existe un emparejamiento simple compatible "
            "con las cuotas y la cardinalidad solicitada."
        )

    left_degrees = _balanced_degree_sequence(
        len(lefts), count, left_min, left_max, "izquierda", table_name, rng
    )
    right_degrees = _balanced_degree_sequence(
        len(rights), count, right_min, right_max, "derecha", table_name, rng
    )
    work.bridge_quota_work += len(lefts) + len(rights)

    left_order = list(range(len(lefts)))
    rng.shuffle(left_order)
    left_order.sort(key=lambda position: -left_degrees[position])
    right_ties = list(range(len(rights)))
    rng.shuffle(right_ties)
    right_rank = {position: rank for rank, position in enumerate(right_ties)}
    right_heap = [
        (-degree, right_rank[position], position) for position, degree in enumerate(right_degrees)
    ]
    heapq.heapify(right_heap)
    pairs: list[tuple[int, int]] = []
    for left_position in left_order:
        selected: list[tuple[int, int, int]] = []
        for _ in range(left_degrees[left_position]):
            work.bridge_quota_work += 1
            if not right_heap or right_heap[0][0] >= 0:
                raise RuntimeError(
                    f"tabla puente {table_name}: no existe un emparejamiento simple "
                    "compatible con ambas cuotas. Ajusta min/max."
                )
            negative_degree, tie, right_position = heapq.heappop(right_heap)
            residual = -negative_degree - 1
            pairs.append((lefts[left_position], rights[right_position]))
            selected.append((residual, tie, right_position))
            work.bridge_quota_work += 1
        for residual, tie, right_position in selected:
            heapq.heappush(right_heap, (-residual, tie, right_position))
            work.bridge_quota_work += 1

    if len(pairs) != count or any(negative_degree != 0 for negative_degree, _, _ in right_heap):
        raise RuntimeError(
            f"tabla puente {table_name}: no existe un emparejamiento simple compatible "
            "con ambas cuotas. Ajusta min/max."
        )
    return pairs


def _balanced_degree_sequence(
    parent_count: int,
    total: int,
    minimum: int,
    maximum: int,
    side: str,
    table_name: str,
    rng: Random,
) -> list[int]:
    """Return a near-regular degree sequence inside uniform bounds."""
    if minimum > maximum or total < parent_count * minimum or total > parent_count * maximum:
        raise RuntimeError(
            f"tabla puente {table_name}: las cuotas de la parte {side} no permiten "
            f"asignar {total} pares entre {parent_count} padres. Ajusta min/max."
        )
    base, remainder = divmod(total, parent_count)
    if base < minimum or base + bool(remainder) > maximum:
        raise RuntimeError(
            f"tabla puente {table_name}: no existe una secuencia de grados factible para "
            f"la parte {side}. Ajusta min/max."
        )
    degrees = [base] * parent_count
    positions = list(range(parent_count))
    rng.shuffle(positions)
    for position in positions[:remainder]:
        degrees[position] += 1
    return degrees


def _bridge_degree_bounds(state: _RelationState | None, opposite_count: int) -> tuple[int, int]:
    """Return per-parent degree bounds, including simple-edge capacity."""
    if state is None:
        return 0, opposite_count
    if getattr(state.relation, "strategy", "quota") == "unique_subset":
        return 0, min(1, opposite_count)
    minimum = int(state.relation.params["min"])
    maximum = int(state.relation.params["max"])
    if minimum > opposite_count:
        raise RuntimeError(
            f"la FK ({', '.join(state.key)}) exige min={minimum}, pero cada padre "
            f"solo admite {opposite_count} pares únicos."
        )
    return minimum, min(maximum, opposite_count)


def _is_degree_limited(state: _RelationState) -> bool:
    """Return whether a FK strategy imposes a per-parent degree bound."""
    return state.relation.strategy in {"quota", "unique_subset"}


def _marker(value: Any) -> Any:
    """Convierte valores de columnas en claves agrupables."""
    if isinstance(value, list):
        return tuple(_marker(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _marker(item)) for key, item in value.items()))
    return value
