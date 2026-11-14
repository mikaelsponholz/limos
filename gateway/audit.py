"""Canonical hashing and append-only per-request decision chains."""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime

from pydantic import JsonValue

from gateway.errors import GatewayError
from gateway.schemas import Actor, Audit, Step, StepType
from gateway.store.base import Store


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_step(
    store: Store,
    request_id: str,
    actor: Actor,
    type: StepType,
    title: str,
    detail: dict[str, JsonValue],
    model: str | None,
    now: datetime,
) -> Step:
    with store.transaction():
        buyer_id = store.request_buyer(request_id)
        if buyer_id is None:
            raise GatewayError(404, "NOT_FOUND", "Request not found.")
        steps = store.get_steps(request_id, buyer_id)
        step = Step(
            seq=len(steps) + 1,
            at=now,
            actor=actor,
            type=type,
            title=title,
            detail=detail,
            model=model,
            prev_hash=steps[-1].hash if steps else None,
            hash="",
        )
        step.hash = canonical_hash(step.model_dump(mode="json", exclude={"hash"}))
        store.append_step(request_id, buyer_id, step)
        return step


def verify_chain(steps: Sequence[Step]) -> bool:
    previous = None
    for seq, step in enumerate(steps, 1):
        if step.seq != seq or step.prev_hash != previous:
            return False
        if step.hash != canonical_hash(step.model_dump(mode="json", exclude={"hash"})):
            return False
        previous = step.hash
    return True


def get_audit(store: Store, request_id: str, buyer_id: str) -> Audit:
    with store.transaction():
        if store.request_buyer(request_id) != buyer_id:
            raise GatewayError(404, "NOT_FOUND", "Request not found.")
        steps = store.get_steps(request_id, buyer_id)
        return Audit(request_id=request_id, chain_valid=verify_chain(steps), steps=steps)
