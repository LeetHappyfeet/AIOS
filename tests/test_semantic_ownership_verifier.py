from uuid import uuid4

from aios_app.epistemic.ownership_verifier import (
    OwnershipCandidate,
    build_adversarial_matrix,
    score_adversarial_matrix,
)


def test_character_memory_survives_adversarial_world_challenge():
    world_id = uuid4()
    character = OwnershipCandidate(
        owner_kind="character",
        owner_key="char:Renamon",
        character_id="Renamon",
        world_id=world_id,
    )
    world = OwnershipCandidate(
        owner_kind="world",
        owner_key=f"world:{world_id}:observed",
        world_id=world_id,
    )
    row = {
        "claim_kind": "MEMORY",
        "predicate_family": "MEMORY",
        "epistemic_scope": "character",
        "origin_character_id": "Renamon",
        "world_id": world_id,
    }

    matrix = build_adversarial_matrix(
        row,
        [character, world],
        vector_totals={character.owner_key: 2.8, world.owner_key: 0.7},
    )
    verification = score_adversarial_matrix(
        matrix,
        proposed_owner_key=character.owner_key,
    )

    assert verification.status == "verified"
    assert verification.winner_owner_key == character.owner_key
    assert verification.margin >= 3
    assert verification.stability >= 0.75
    assert matrix[world.owner_key]["counterfactual"] < 0


def test_narrative_event_challenges_character_ownership():
    world_id = uuid4()
    character = OwnershipCandidate(
        owner_kind="character",
        owner_key="char:Renamon",
        character_id="Renamon",
        world_id=world_id,
    )
    world = OwnershipCandidate(
        owner_kind="world",
        owner_key=f"world:{world_id}:observed",
        world_id=world_id,
    )
    row = {
        "claim_kind": "EVENT",
        "predicate_family": "ACTION",
        "epistemic_scope": "narrative",
        "origin_character_id": "Renamon",
        "world_id": world_id,
    }

    matrix = build_adversarial_matrix(
        row,
        [character, world],
        vector_totals={character.owner_key: 0.7, world.owner_key: 3.1},
    )
    verification = score_adversarial_matrix(
        matrix,
        proposed_owner_key=character.owner_key,
    )

    assert verification.status == "challenged"
    assert verification.winner_owner_key == world.owner_key
    assert matrix[character.owner_key]["counterfactual"] < 0
    assert matrix[world.owner_key]["counterfactual"] > 0


def test_adversarial_matrix_refuses_to_invent_certainty_from_tie():
    world_id = uuid4()
    character = OwnershipCandidate(
        owner_kind="character",
        owner_key="char:Renamon",
        character_id="Renamon",
        world_id=world_id,
    )
    world = OwnershipCandidate(
        owner_kind="world",
        owner_key=f"world:{world_id}:observed",
        world_id=world_id,
    )

    matrix = build_adversarial_matrix({}, [character, world])
    verification = score_adversarial_matrix(
        matrix,
        proposed_owner_key=character.owner_key,
    )

    assert verification.status == "fragile"
    assert verification.winner_score < 4
    assert verification.margin < 3


def test_cross_world_candidate_receives_decisive_world_penalty():
    current_world = uuid4()
    wrong_world = uuid4()
    candidate = OwnershipCandidate(
        owner_kind="world",
        owner_key=f"world:{wrong_world}:observed",
        world_id=wrong_world,
    )

    matrix = build_adversarial_matrix(
        {
            "epistemic_scope": "narrative",
            "world_id": current_world,
            "claim_kind": "EVENT",
            "predicate_family": "ACTION",
        },
        [candidate],
    )

    assert matrix[candidate.owner_key]["world"] == -3
