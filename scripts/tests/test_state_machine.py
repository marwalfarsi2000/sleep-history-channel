import pytest
from state_machine import State, normalize_state, validate_transition, transition_state


def test_normalize_legacy_states():
    assert normalize_state("drafting") == State.GENERATING
    assert normalize_state("READY_FOR_APPROVAL") == State.MEDIA_READY
    assert normalize_state("pending_approval") == State.REVIEW_PENDING
    assert normalize_state("COMPLETED") == State.PUBLISHED
    assert normalize_state("REJECTED_DELETED") == State.REJECTED


def test_valid_transitions():
    assert validate_transition(State.GENERATING, State.MEDIA_READY)
    assert validate_transition(State.MEDIA_READY, State.REVIEW_PENDING)
    assert validate_transition(State.REVIEW_PENDING, State.APPROVED_FOR_PUBLISH)
    assert validate_transition(State.APPROVED_FOR_PUBLISH, State.PUBLISHING)
    assert validate_transition(State.PUBLISHING, State.PUBLISHED)


def test_invalid_transitions():
    assert not validate_transition(State.PUBLISHED, State.GENERATING)
    assert not validate_transition(State.REJECTED, State.PUBLISHED)
    with pytest.raises(ValueError):
        transition_state(State.PUBLISHED, State.GENERATING)
