"""SillyTavern lorebook parser regression tests (no database required)."""
import json

import pytest

from aios_app.lorebook_import import parse_lorebook


def _write(tmp_path, obj, filename="lore.json"):
    path = tmp_path / filename
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_world_info_preserves_entries_and_metadata(tmp_path):
    path = _write(tmp_path, {
        "name": "Equestria",
        "entries": {
            "12": {
                "uid": 12, "comment": "Ponyville", "key": ["Ponyville"],
                "content": "A town in Equestria.", "order": 120, "disable": False,
            },
            "13": {
                "uid": 13, "comment": "Old town",
                "content": "An abandoned draft.", "disable": True,
            },
        },
    })
    parsed = parse_lorebook(path)
    assert parsed["title"] == "Equestria"
    assert parsed["format"] == "world_info"
    assert parsed["total_entries"] == 2
    assert parsed["disabled_entries"] == 1
    assert len(parsed["sections"]) == 1
    section = parsed["sections"][0]
    assert section["heading"] == "Ponyville"
    assert section["meta"]["keys"] == ["Ponyville"]
    assert section["meta"]["sillytavern"]["order"] == 120
    assert "content" not in section["meta"]["sillytavern"]
    assert len(parse_lorebook(path, include_disabled=True)["sections"]) == 2


def test_character_book_array_and_embedded_card(tmp_path):
    book = {
        "name": "My Little Pony",
        "entries": [
            {"name": "Twilight Sparkle", "keys": ["Twilight"],
             "content": "A unicorn who studies friendship.", "enabled": True},
        ],
    }
    standalone = parse_lorebook(_write(tmp_path, book, "standalone.json"))
    embedded = parse_lorebook(_write(
        tmp_path, {"data": {"character_book": book}}, "embedded.json"
    ))
    assert standalone["format"] == "character_book"
    assert embedded["format"] == "character_book"
    assert standalone["sections"][0]["meta"]["keys"] == ["Twilight"]
    assert standalone["sections"][0]["content"] == embedded["sections"][0]["content"]


def test_rejects_malformed_or_empty_book(tmp_path):
    with pytest.raises(ValueError, match="No SillyTavern entries"):
        parse_lorebook(_write(tmp_path, {"character": "Renamon"}))
    with pytest.raises(ValueError, match="no importable"):
        parse_lorebook(_write(tmp_path, {
            "entries": {"1": {"content": " ", "disable": False}}
        }))
    with pytest.raises(ValueError, match="not an object"):
        parse_lorebook(_write(tmp_path, {"entries": {"1": 7}}))


def test_source_hash_is_stable(tmp_path):
    path = _write(tmp_path, {"entries": {"1": {"content": "Canterlot."}}})
    assert parse_lorebook(path)["sha256"] == parse_lorebook(path)["sha256"]
