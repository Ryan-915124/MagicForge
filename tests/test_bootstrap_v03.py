from types import SimpleNamespace

import pytest

from retrieval.bootstrap_v03 import _excluded_source_ids, _storage_safe


def _projection(*, role: str, sensitivity: str):
    return SimpleNamespace(
        claim_roles=[SimpleNamespace(value=role)],
        sensitive_information_level=SimpleNamespace(value=sensitivity),
    )


def test_v03_quarantines_unreviewed_controlled_methods() -> None:
    assert not _storage_safe(_projection(role="method", sensitivity="controlled"))
    assert not _storage_safe(_projection(role="method", sensitivity="restricted"))
    assert _storage_safe(_projection(role="method", sensitivity="public"))
    assert _storage_safe(_projection(role="result", sensitivity="controlled"))


def test_v03_safety_exclusions_validate_uuid_values(tmp_path) -> None:
    path = tmp_path / "exclusions.json"
    path.write_text(
        '{"source_candidate_ids":["145054b4-a90a-551c-a73e-90a47c55c5b4"]}'
    )
    assert _excluded_source_ids(path) == {
        "145054b4-a90a-551c-a73e-90a47c55c5b4"
    }

    path.write_text('{"source_candidate_ids":["not-a-uuid"]}')
    with pytest.raises(ValueError):
        _excluded_source_ids(path)
