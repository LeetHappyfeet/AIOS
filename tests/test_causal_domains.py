from datetime import datetime, timezone
from uuid import uuid4

from aios_app.causal.domains.location import LocationDomain
from aios_app.causal.domains.scalar import ScalarStateDomain
from aios_app.causal.types import (
    AdmissionDecision,
    CausalCandidate,
    CausalState,
)


def _candidate(*, domain_id, event_type, state_key, value, node=None, occurred_at=None, params=None):
    return CausalCandidate(
        world_id=uuid4(),
        timeline_id=uuid4(),
        domain_id=domain_id,
        event_type=event_type,
        entity_id=uuid4(),
        state_key=state_key,
        value=value,
        dag_node_id=node,
        occurred_at=occurred_at,
        parameters=params or {},
    )


def test_location_rejects_two_locations_at_same_coordinate():
    node = uuid4()
    old_location = str(uuid4())
    new_location = str(uuid4())
    state = CausalState(value=old_location, version=4, last_node_id=node)
    candidate = _candidate(
        domain_id="world.location",
        event_type="assert_location",
        state_key="location_entity_id",
        value=new_location,
        node=node,
    )

    result = LocationDomain().evaluate(state, candidate, rules=[])

    assert result.decision is AdmissionDecision.REJECTED_IMPOSSIBLE
    assert result.before == old_location
    assert result.after == new_location


def test_location_explicit_move_is_a_legal_transition():
    old_location = str(uuid4())
    new_location = str(uuid4())
    state = CausalState(value=old_location, version=7, last_node_id=uuid4())
    candidate = _candidate(
        domain_id="world.location",
        event_type="move",
        state_key="location_entity_id",
        value=new_location,
        node=uuid4(),
    )

    result = LocationDomain().evaluate(state, candidate, rules=[])

    assert result.decision is AdmissionDecision.ADMITTED
    assert result.delta == {"location": [old_location, new_location]}


def test_location_can_record_omitted_movement_without_inventing_mechanism():
    first = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    second = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
    old_location = str(uuid4())
    new_location = str(uuid4())
    state = CausalState(value=old_location, version=2, occurred_at=first)
    candidate = _candidate(
        domain_id="world.location",
        event_type="assert_location",
        state_key="location_entity_id",
        value=new_location,
        occurred_at=second,
        params={"allow_latent_transition": True},
    )

    result = LocationDomain().evaluate(state, candidate, rules=[])

    assert result.decision is AdmissionDecision.ADMITTED_WITH_LATENT_TRANSITION
    assert result.latent_events[0]["mechanism"] == "unknown"


def test_scalar_rejects_conflicting_sensor_values_at_same_instant():
    instant = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    state = CausalState(value={"value": 19.2, "unit": "C"}, version=12, occurred_at=instant)
    candidate = _candidate(
        domain_id="world.scalar",
        event_type="measurement",
        state_key="room_temperature",
        value={"value": 22.8, "unit": "C"},
        occurred_at=instant,
    )

    result = ScalarStateDomain().evaluate(state, candidate, rules=[])

    assert result.decision is AdmissionDecision.CONFLICT


def test_scalar_accepts_ordered_measurements():
    first = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)
    second = datetime(2026, 9, 11, 8, 1, tzinfo=timezone.utc)
    state = CausalState(value=12.4, version=1, occurred_at=first)
    candidate = _candidate(
        domain_id="world.scalar",
        event_type="measurement",
        state_key="battery_voltage",
        value=12.3,
        occurred_at=second,
    )

    result = ScalarStateDomain().evaluate(state, candidate, rules=[])

    assert result.decision is AdmissionDecision.ADMITTED
