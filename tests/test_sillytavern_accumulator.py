import json
from pathlib import Path

from aios_app.accumulator.sillytavern.ingestor import SillyTavernChatIngestor
from aios_app.accumulator.sillytavern.parser import parse_sillytavern_jsonl


def _write_chat(path: Path) -> None:
    rows = [
        {
            "user_name": "Ren-119",
            "character_name": "Zeke-134",
            "create_date": "2025-05-17@20h48m40s",
            "chat_metadata": {
                "integrity": "95a13e8a-14d9-4d42-9c6c-0ed41065af67",
                "chat_id_hash": 4148569770736994,
                "tainted": True,
            },
        },
        {
            "name": "Ren-119",
            "is_user": True,
            "is_system": False,
            "send_date": "May 17, 2025 8:51pm",
            "mes": "Ren gets up. It is a normal day.",
            "extra": {"reasoning": ""},
        },
        {
            "name": "Zeke-134",
            "is_user": False,
            "send_date": "May 17, 2025 8:52pm",
            "mes": "Warning, memory cores damaged.",
            "gen_started": "2025-05-18T00:52:38.145Z",
            "gen_finished": "2025-05-18T00:52:56.288Z",
            "swipe_id": 0,
            "swipes": ["Warning, memory cores damaged."],
            "swipe_info": [],
            "extra": {
                "api": "mancer",
                "model": "magnum-72b-v4",
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_sillytavern_parser_reads_header_and_speaker_roles(tmp_path):
    path = tmp_path / "chat.jsonl"
    _write_chat(path)

    parsed = parse_sillytavern_jsonl(path)

    assert parsed.user_name == "Ren-119"
    assert parsed.character_name == "Zeke-134"
    assert parsed.chat_key == "95a13e8a-14d9-4d42-9c6c-0ed41065af67"
    assert len(parsed.messages) == 2

    user, character = parsed.messages
    assert user.speaker_id == "Ren-119"
    assert user.speaker_type == "user"
    assert user.viewpoint_id == "Ren-119"

    assert character.speaker_id == "Zeke-134"
    assert character.speaker_type == "character"
    assert character.viewpoint_id == "Zeke-134"


def test_local_send_date_is_not_given_a_fake_timezone(tmp_path):
    path = tmp_path / "chat.jsonl"
    _write_chat(path)
    parsed = parse_sillytavern_jsonl(path)

    assert parsed.messages[0].event_time is None
    assert parsed.messages[1].event_time is not None
    assert parsed.messages[1].event_time.utcoffset() is not None


def test_selected_message_is_primary_and_swipes_remain_provenance(tmp_path):
    path = tmp_path / "chat.jsonl"
    _write_chat(path)
    parsed = parse_sillytavern_jsonl(path)
    message = parsed.messages[1]

    ingestor = SillyTavernChatIngestor(None)
    payload = ingestor._message_payload(
        parsed,
        message,
        "sillytavern:95a13e8a-14d9-4d42-9c6c-0ed41065af67",
    )

    assert payload["text"] == "Warning, memory cores damaged."
    assert payload["swipe_id"] == 0
    assert payload["swipe_count"] == 1
    assert payload["swipes"] == ["Warning, memory cores damaged."]
    assert payload["extra"]["model"] == "magnum-72b-v4"


def test_missing_header_identity_is_rejected(tmp_path):
    path = tmp_path / "invalid.jsonl"
    path.write_text(
        json.dumps({"chat_metadata": {}}) + "\n"
        + json.dumps({"name": "Someone", "mes": "Hello"}) + "\n",
        encoding="utf-8",
    )

    try:
        parse_sillytavern_jsonl(path)
    except ValueError as exc:
        assert "user_name and character_name" in str(exc)
    else:
        raise AssertionError("invalid SillyTavern log should be rejected")
