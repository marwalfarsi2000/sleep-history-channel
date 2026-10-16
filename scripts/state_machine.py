"""Centralized canonical state machine for sleep history channel automation."""

from __future__ import annotations


class State:
    GENERATING = "GENERATING"
    MEDIA_READY = "MEDIA_READY"
    REVIEW_PENDING = "REVIEW_PENDING"
    APPROVED_FOR_PUBLISH = "APPROVED_FOR_PUBLISH"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"


LEGACY_STATE_MAP = {
    "drafting": State.GENERATING,
    "generating": State.GENERATING,
    "regenerating": State.GENERATING,
    "READY_FOR_APPROVAL": State.MEDIA_READY,
    "pending_approval": State.REVIEW_PENDING,
    "REVIEW_PENDING": State.REVIEW_PENDING,
    "APPROVED_FOR_PUBLISH": State.APPROVED_FOR_PUBLISH,
    "publishing": State.PUBLISHING,
    "published": State.PUBLISHED,
    "COMPLETED": State.PUBLISHED,
    "rejected": State.REJECTED,
    "rejected_deleted": State.REJECTED,
    "REJECTED_DELETED": State.REJECTED,
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    State.GENERATING: {State.MEDIA_READY, State.FAILED_RETRYABLE, State.GENERATING},
    State.MEDIA_READY: {State.REVIEW_PENDING, State.GENERATING, State.FAILED_RETRYABLE},
    State.REVIEW_PENDING: {State.APPROVED_FOR_PUBLISH, State.REJECTED, State.GENERATING, State.FAILED_RETRYABLE},
    State.APPROVED_FOR_PUBLISH: {State.PUBLISHING, State.PUBLISHED, State.FAILED_RETRYABLE},
    State.PUBLISHING: {State.PUBLISHED, State.FAILED_RETRYABLE, State.APPROVED_FOR_PUBLISH},
    State.PUBLISHED: set(),
    State.REJECTED: set(),
    State.FAILED_RETRYABLE: {State.GENERATING, State.REVIEW_PENDING, State.APPROVED_FOR_PUBLISH, State.PUBLISHING},
}


def normalize_state(raw_state: str) -> str:
    if not raw_state:
        return State.GENERATING
    state_str = str(raw_state).strip()
    return LEGACY_STATE_MAP.get(state_str, state_str)


def validate_transition(from_state: str, to_state: str) -> bool:
    current = normalize_state(from_state)
    target = normalize_state(to_state)
    if current == target:
        return True
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    return target in allowed


def transition_state(from_state: str, to_state: str) -> str:
    target = normalize_state(to_state)
    if not validate_transition(from_state, to_state):
        raise ValueError(f"Invalid state transition from {from_state!r} to {to_state!r}")
    return target
