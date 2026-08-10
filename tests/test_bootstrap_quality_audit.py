from __future__ import annotations

import pytest

from research.bootstrap.quality_audit import _judge_complete_batches


def test_quality_audit_recursively_splits_incomplete_batches() -> None:
    calls: list[list[str]] = []
    items = [{"id": str(index)} for index in range(5)]

    def judge(_llm, batch):
        ids = [str(item["id"]) for item in batch]
        calls.append(ids)
        if len(batch) > 2:
            raise ValueError("model omitted an id")
        return [{"id": item["id"], "claim_supported": True} for item in batch]

    decisions = _judge_complete_batches(None, items, judge, batch_size=5)

    assert [item["id"] for item in decisions] == ["0", "1", "2", "3", "4"]
    assert calls[0] == ["0", "1", "2", "3", "4"]
    assert all(item["claim_supported"] for item in decisions)


def test_quality_audit_fails_closed_for_single_item_id_mismatch() -> None:
    def judge(_llm, _batch):
        raise ValueError("model omitted the only id")

    with pytest.raises(ValueError, match="only id"):
        _judge_complete_batches(None, [{"id": "one"}], judge)
