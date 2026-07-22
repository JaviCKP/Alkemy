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
from collections.abc import Callable, Sequence
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
        if self.table_kind == "bridge" and len(self.relations) >= 2:
            assignments = self._assign_bridge()
        else:
            assignments = [{} for _ in range(self.total)]
            for row_index, rng in enumerate(self.row_rngs):
                assignments[row_index] = self.assign_row(row_index, rng)
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
        if self.table_kind == "bridge" and len(self.relations) >= 2:
            self._prepared = True
            return

        self._prepare_null_masks()
        for component in self.components:
            self._prepare_component(component, validate_only=validate_only)
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
            if sum(state.relation.strategy == "quota" for state in component.states) > 1:
                quota_states = [
                    state for state in component.states if state.relation.strategy == "quota"
                ]
                ratios = {
                    float(state.relation.params.get("null_ratio", 0.0)) for state in quota_states
                }
                if len(ratios) > 1 and any(
                    int(state.relation.params["min"]) > 0 for state in quota_states
                ):
                    labels = _relations_label(quota_states)
                    raise self._error(
                        f"tabla {self.table_name}: las cuotas compartidas {labels} usan "
                        "null_ratio distintos y min>0; la asignación conjunta de sus "
                        "patrones NULL no es compatible con este asignador. Usa el mismo "
                        "null_ratio o min=0 en las relaciones compartidas."
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

    def _prepare_component(self, component: _Component, *, validate_only: bool) -> None:
        if len(component.states) == 1:
            self._prepare_single_quota(component.states[0])
            return

        common = set(component.states[0].relation.fk.columns)
        for state in component.states[1:]:
            common.intersection_update(state.relation.fk.columns)
        component.discriminator = tuple(sorted(common))
        for state in component.states:
            state.groups = _group_parent_indices(state.relation, component.discriminator)

        quota_states = [state for state in component.states if state.relation.strategy == "quota"]
        if quota_states and not validate_only:
            self._prepare_quota_component(component, quota_states)
        elif quota_states:
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

    def _prepare_quota_component(
        self, component: _Component, quota_states: list[_RelationState]
    ) -> None:
        active_sets = {
            tuple(index for index, is_null in enumerate(state.null_rows) if not is_null)
            for state in quota_states
        }
        if len(quota_states) == 1 or len(active_sets) == 1:
            active_rows = list(next(iter(active_sets))) if active_sets else []
            self._prepare_fixed_group_quota(component, quota_states, active_rows)
            return

        # Different nullable quotas need independent NULL masks. With min=0 the
        # group assignment can be made row by row while tracking every max.
        if any(int(state.relation.params["min"]) > 0 for state in quota_states):
            raise self._error(
                f"tabla {self.table_name}: las cuotas compartidas "
                f"{_relations_label(quota_states)} "
                "han producido patrones NULL distintos con min>0. La combinación no se "
                "puede certificar sin relajar una cuota; fija un patrón común o usa min=0."
            )
        component.group_labels = [None] * self.total
        remaining_capacity: dict[FkKey, dict[tuple[Any, ...], int]] = {
            state.key: {
                group: len(indices) * int(state.relation.params["max"])
                for group, indices in state.groups.items()
            }
            for state in quota_states
        }
        for row_index, rng in enumerate(self.row_rngs):
            active = [
                state
                for state in component.states
                if not state.relation.unresolved
                and not state.relation.allow_missing_parents
                and state.relation.parent_rows
                and not state.null_rows[row_index]
            ]
            constraining = [
                state
                for state in active
                if state.relation.strategy == "quota" or not state.relation.nullable_columns
            ]
            group_keys = self._candidate_groups(component, constraining or active, None)
            if not group_keys:
                if active:
                    raise self._incompatible_row_error(component, row_index)
                continue
            usable = [
                group
                for group in group_keys
                if all(
                    state.null_rows[row_index]
                    or group not in remaining_capacity[state.key]
                    or remaining_capacity[state.key][group] > 0
                    for state in quota_states
                )
            ]
            if not usable:
                raise self._error(
                    f"tabla {self.table_name}: las cuotas compartidas se quedan sin capacidad "
                    f"en la fila {row_index}; revisa max y las relaciones que comparten "
                    "columnas."
                )
            group = usable[rng.randrange(len(usable))]
            component.group_labels[row_index] = group
            for state in quota_states:
                if not state.null_rows[row_index]:
                    remaining_capacity[state.key][group] -= 1
        self._fill_group_quota_assignments(component, quota_states)

    def _prepare_fixed_group_quota(
        self,
        component: _Component,
        quota_states: list[_RelationState],
        active_rows: list[int],
    ) -> None:
        required_states = [
            state
            for state in component.states
            if any(
                not state.relation.unresolved
                and not state.relation.allow_missing_parents
                and state.relation.parent_rows
                and not state.null_rows[row_index]
                for row_index in active_rows
            )
            and (state.relation.strategy == "quota" or not state.relation.nullable_columns)
        ]
        common_groups = self._candidate_groups(component, required_states, None)
        ranges: dict[tuple[Any, ...], tuple[int, int]] = {}
        for group in common_groups:
            intervals: list[tuple[int, int]] = []
            for state in quota_states:
                parents = state.groups.get(group, [])
                if not parents:
                    intervals = []
                    break
                minimum = int(state.relation.params["min"])
                maximum = int(state.relation.params["max"])
                intervals.append((len(parents) * minimum, len(parents) * maximum))
            if not intervals:
                continue
            low = max(interval[0] for interval in intervals)
            high = min(interval[1] for interval in intervals)
            if low <= high:
                ranges[group] = (low, high)
            elif any(interval[0] > 0 for interval in intervals):
                raise self._quota_component_error(component, quota_states, group)

        for state in quota_states:
            if int(state.relation.params["min"]) <= 0:
                continue
            missing = [group for group in state.groups if group not in ranges]
            if missing:
                raise self._quota_component_error(component, quota_states, missing[0])

        needed = len(active_rows)
        if not ranges:
            if needed == 0 and all(
                int(state.relation.params["min"]) == 0 for state in quota_states
            ):
                component.group_labels = [None] * self.total
                for state in quota_states:
                    state.quota = [-1] * self.total
                return
            raise self._error(
                f"tabla {self.table_name}: las cuotas compartidas {_relations_label(quota_states)} "
                f"no tienen grupos compatibles para alojar {needed} filas. Añade padres "
                "compatibles o ajusta min/max."
            )
        low_total = sum(low for low, _ in ranges.values())
        high_total = sum(high for _, high in ranges.values())
        if not low_total <= needed <= high_total:
            raise self._error(
                f"tabla {self.table_name}: las cuotas compartidas {_relations_label(quota_states)} "
                f"no pueden alojar conjuntamente {needed} hijos; el total factible está en "
                f"[{low_total}, {high_total}]. Ajusta las cuotas, los padres o las filas."
            )

        order = _stable_group_order(ranges)
        group_rng = Random(seed_for_table(self.seed, f"{self.table_name}:quota-groups"))
        counts = _distribute_counts(order, ranges, needed, group_rng)
        labels: list[tuple[Any, ...]] = []
        for group in order:
            labels.extend([group] * counts[group])
        group_rng.shuffle(labels)
        component.group_labels = [None] * self.total
        for row_index, group in zip(active_rows, labels, strict=True):
            component.group_labels[row_index] = group
        self._fill_group_quota_assignments(component, quota_states)

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
    ) -> dict[FkKey, int | None]:
        if locked and any(
            self.deferred_null_columns.intersection(state.relation.fk.columns)
            for state in component.states
        ):
            return {state.key: None for state in component.states}
        if len(component.states) == 1:
            state = component.states[0]
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
        for state in component.states:
            if (
                state.relation.unresolved
                or (state.relation.allow_missing_parents and not state.relation.parent_rows)
                or state.null_rows[row_index]
            ):
                assignments[state.key] = None
                continue
            candidates = self._candidate_indices(state, group, initial_values)
            selected: int | None
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

        left_quota = first.relation.strategy == "quota"
        right_quota = second.relation.strategy == "quota"
        bridge_rng = Random(seed_for_table(self.seed, f"{self.table_name}:bridge"))
        try:
            if not left_quota and not right_quota:
                pairs = _sample_uniform_pairs(groups, self.total, bridge_rng)
            else:
                pairs = self._quota_bridge_pairs(
                    first, second, groups, self.total, bridge_rng, left_quota, right_quota
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
        left_quota: bool,
        right_quota: bool,
    ) -> list[tuple[int, int]]:
        ranges: dict[tuple[Any, ...], tuple[int, int]] = {}
        for group, (lefts, rights) in groups.items():
            capacity = len(lefts) * len(rights)
            intervals = [(0, capacity)]
            if left_quota:
                intervals.append(
                    _quota_group_interval(first, len(lefts), len(rights), self.table_name)
                )
            if right_quota:
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
                    first if left_quota else None,
                    second if right_quota else None,
                    rng,
                    self.table_name,
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


def _quota_group_interval(
    state: _RelationState,
    parent_count: int,
    opposite_count: int,
    table_name: str,
) -> tuple[int, int]:
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
) -> list[tuple[int, int]]:
    if count == 0:
        return []
    if left_quota is not None:
        left_assignment = build_quota_assignment(
            rng,
            len(lefts),
            count,
            int(left_quota.relation.params["min"]),
            min(int(left_quota.relation.params["max"]), len(rights)),
        )
        left_degrees = _quota_degree_counts(left_assignment, len(lefts))
    else:
        left_degrees = None
    if right_quota is not None:
        right_assignment = build_quota_assignment(
            rng,
            len(rights),
            count,
            int(right_quota.relation.params["min"]),
            min(int(right_quota.relation.params["max"]), len(lefts)),
        )
        right_degrees = _quota_degree_counts(right_assignment, len(rights))
    else:
        right_degrees = None

    if left_degrees is not None and right_degrees is not None:
        return _match_degrees(lefts, left_degrees, rights, right_degrees, rng, table_name)
    if left_degrees is not None:
        pairs: list[tuple[int, int]] = []
        for left, degree in zip(lefts, left_degrees, strict=True):
            pairs.extend((left, right) for right in rng.sample(rights, degree))
        return pairs
    if right_degrees is not None:
        pairs = []
        for right, degree in zip(rights, right_degrees, strict=True):
            pairs.extend((left, right) for left in rng.sample(lefts, degree))
        return pairs
    raise AssertionError("grupo de puente sin cuota en la ruta de cuotas")


def _quota_degree_counts(assignment: Sequence[int], n_parents: int) -> list[int]:
    """Convierte una secuencia de padres de cuota en grados por padre."""
    counts = [0] * n_parents
    for parent in assignment:
        counts[parent] += 1
    return counts


def _match_degrees(
    lefts: list[int],
    left_degrees: list[int],
    rights: list[int],
    right_degrees: list[int],
    rng: Random,
    table_name: str,
) -> list[tuple[int, int]]:
    if sum(left_degrees) != sum(right_degrees):
        raise RuntimeError(
            f"tabla puente {table_name}: las cuotas de ambos lados no suman el mismo "
            "número de pares. Ajusta min/max."
        )
    right_heap: list[tuple[int, int, int]] = []
    tie = list(range(len(rights)))
    rng.shuffle(tie)
    tie_by_position = dict(enumerate(tie))
    for position, degree in enumerate(right_degrees):
        if degree:
            heapq.heappush(right_heap, (-degree, tie_by_position[position], position))
    left_order = list(range(len(lefts)))
    rng.shuffle(left_order)
    left_order.sort(key=lambda position: left_degrees[position], reverse=True)
    pairs: list[tuple[int, int]] = []
    for left_position in left_order:
        degree = left_degrees[left_position]
        selected: list[tuple[int, int, int]] = []
        for _ in range(degree):
            if not right_heap:
                raise RuntimeError(
                    f"tabla puente {table_name}: no existe un emparejamiento simple "
                    "compatible con ambas cuotas. Ajusta min/max."
                )
            item = heapq.heappop(right_heap)
            selected.append(item)
            pairs.append((lefts[left_position], rights[item[2]]))
        for degree_left, tie_value, position in selected:
            residual = degree_left + 1
            if residual > 0:
                raise RuntimeError("grado positivo durante el emparejamiento del puente")
            if residual:
                heapq.heappush(right_heap, (residual, tie_value, position))
    if right_heap:
        raise RuntimeError(
            f"tabla puente {table_name}: las cuotas dejan padres derechos sin emparejar. "
            "Ajusta min/max."
        )
    return pairs


def _marker(value: Any) -> Any:
    """Convierte valores de columnas en claves agrupables."""
    if isinstance(value, list):
        return tuple(_marker(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _marker(item)) for key, item in value.items()))
    return value
