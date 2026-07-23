from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from baseline import build_baseline
from ruamel.yaml import YAML

_FIXTURES = Path(__file__).parent / "fixtures"


def test_recorded_baseline_matches_common_evaluation_route() -> None:
    recorded: dict[str, Any] = json.loads((_FIXTURES / "baseline_v1.json").read_text("utf-8"))

    assert build_baseline() == recorded


def test_every_source_uses_the_exact_same_population() -> None:
    baseline = build_baseline()

    assert baseline["population"]["unique_columns"] == 85
    assert baseline["metrics"]["heuristic"]["role"]["total"] == 85
    assert baseline["metrics"]["heuristic"]["generator"]["total"] == 85
    for metrics in baseline["metrics"]["h0"].values():
        assert metrics["role"]["total"] == 255
        assert metrics["generator"]["total"] == 255


def test_human_label_review_is_prepared_but_still_pending() -> None:
    review: dict[str, Any] = YAML(typ="safe").load(
        (_FIXTURES / "labels_review_v1.yaml").read_text("utf-8")
    )

    assert review["status"] == "pending_human_second_review"
    assert review["reviewer"] is None
    assert review["reviewed_at"] is None
    assert review["decision"] == "pending"
    assert len(review["population"]) == 6
    assert all(len(item["sha256"]) == 64 for item in review["population"])
