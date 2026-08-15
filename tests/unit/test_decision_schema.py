import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / 'src' / 'djlib' / 'report' / 'decision-schema.json'
)


@pytest.fixture(scope='module')
def schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding='utf-8'))


def _envelope(*decisions: dict[str, Any]) -> dict[str, Any]:
    return {
        'schema_version': 1,
        'report_id': 'rpt_abc123',
        'catalog_revision': 'rev_deadbeef',
        'generated_at': '2026-08-15T12:00:00Z',
        'decisions': list(decisions),
    }


def _validate(schema: dict[str, Any], instance: dict[str, Any]) -> None:
    jsonschema.validate(instance=instance, schema=schema)


# -- valid decisions ------------------------------------------------------


def test_confirm_without_preferred_file_id_is_valid(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {'group_id': 'dup_1', 'decision': 'CONFIRM', 'reviewed_at': '2026-08-15T12:00:00Z'}
    )
    _validate(schema, envelope)


def test_reject_without_preferred_file_id_is_valid(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {'group_id': 'dup_1', 'decision': 'REJECT', 'reviewed_at': '2026-08-15T12:00:00Z'}
    )
    _validate(schema, envelope)


def test_defer_without_preferred_file_id_is_valid(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {'group_id': 'dup_1', 'decision': 'DEFER', 'reviewed_at': '2026-08-15T12:00:00Z'}
    )
    _validate(schema, envelope)


def test_change_preferred_with_preferred_file_id_is_valid(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {
            'group_id': 'dup_1',
            'decision': 'CHANGE_PREFERRED',
            'preferred_file_id': 'fil_1',
            'reviewed_at': '2026-08-15T12:00:00Z',
        }
    )
    _validate(schema, envelope)


def test_confirm_may_also_carry_a_preferred_file_id_for_record_keeping(
    schema: dict[str, Any],
) -> None:
    # Design decision (documented in generator.py / decision-schema.json):
    # CONFIRM/REJECT/DEFER may optionally carry the already-agreed
    # `preferred_file_id` for record-keeping -- it is only *required* (and
    # must be non-null) for CHANGE_PREFERRED. It is never required for the
    # other three actions, but a valid non-null `fil_...` value is allowed.
    envelope = _envelope(
        {
            'group_id': 'dup_1',
            'decision': 'CONFIRM',
            'preferred_file_id': 'fil_1',
            'reviewed_at': '2026-08-15T12:00:00Z',
        }
    )
    _validate(schema, envelope)


def test_empty_decisions_list_is_valid(schema: dict[str, Any]) -> None:
    _validate(schema, _envelope())


# -- invalid decisions ------------------------------------------------------


def test_unknown_action_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {'group_id': 'dup_1', 'decision': 'MERGE', 'reviewed_at': '2026-08-15T12:00:00Z'}
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_change_preferred_missing_preferred_file_id_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {'group_id': 'dup_1', 'decision': 'CHANGE_PREFERRED', 'reviewed_at': '2026-08-15T12:00:00Z'}
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_change_preferred_null_preferred_file_id_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {
            'group_id': 'dup_1',
            'decision': 'CHANGE_PREFERRED',
            'preferred_file_id': None,
            'reviewed_at': '2026-08-15T12:00:00Z',
        }
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_missing_group_id_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope({'decision': 'CONFIRM', 'reviewed_at': '2026-08-15T12:00:00Z'})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_missing_reviewed_at_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope({'group_id': 'dup_1', 'decision': 'CONFIRM'})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_missing_decision_field_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope({'group_id': 'dup_1', 'reviewed_at': '2026-08-15T12:00:00Z'})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_wrong_schema_version_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {'group_id': 'dup_1', 'decision': 'CONFIRM', 'reviewed_at': '2026-08-15T12:00:00Z'}
    )
    envelope['schema_version'] = 2
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_malformed_group_id_prefix_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {'group_id': 'trk_1', 'decision': 'CONFIRM', 'reviewed_at': '2026-08-15T12:00:00Z'}
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_unknown_top_level_field_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {'group_id': 'dup_1', 'decision': 'CONFIRM', 'reviewed_at': '2026-08-15T12:00:00Z'}
    )
    envelope['unexpected_field'] = 'nope'
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)


def test_unknown_decision_field_is_rejected(schema: dict[str, Any]) -> None:
    envelope = _envelope(
        {
            'group_id': 'dup_1',
            'decision': 'CONFIRM',
            'reviewed_at': '2026-08-15T12:00:00Z',
            'unexpected_field': 'nope',
        }
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(schema, envelope)
