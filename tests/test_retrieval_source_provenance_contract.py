"""Prevent borrowed claim provenance in character belief/episode retrieval."""
from pathlib import Path


def test_retrieval_uses_current_admitted_source_and_exact_episode_claim():
    source = (
        Path(__file__).resolve().parents[1] / "epistemic/retrieval.py"
    ).read_text(encoding="utf-8")
    assert "sea_auth.status='active'" in source
    assert "aios.semantic_acquisition_source_eligible(kae_auth.acquisition_id)" in source
    assert "obs.claim_id=kae.claim_id AND obs.proposition_id=cpk.proposition_id" in source
    assert "aios.semantic_acquisition_source_eligible(kae.acquisition_id)" in source
