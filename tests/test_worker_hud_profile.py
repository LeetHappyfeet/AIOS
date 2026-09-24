from aios_app.hud.worker_profile import WORKER_HUD_PROFILES, get_worker_hud_profile


def test_worker_hud_profiles_are_micro_and_bounded():
    assert {"attention", "reflection", "recall", "inquiry", "planning", "executive"} <= set(WORKER_HUD_PROFILES)
    for profile in WORKER_HUD_PROFILES.values():
        assert 180 <= profile.token_budget <= 700
        assert 0 <= profile.memory_items <= 3
        assert 0 <= profile.belief_items <= 3
        assert 0 <= profile.goal_items <= 2
        assert 0 <= profile.recent_event_items <= 2


def test_unknown_worker_profile_falls_back_to_attention():
    assert get_worker_hud_profile("does-not-exist") == WORKER_HUD_PROFILES["attention"]
