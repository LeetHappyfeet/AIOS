from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from uuid import UUID

from aios_app.dag import add_node_and_edge, get_or_create_timeline
from aios_app.db import Database

from .config import ACCUMULATOR_ID, SOURCE_KIND
from .parser import ParsedChatLog, ParsedMessage, parse_sillytavern_jsonl

logger = logging.getLogger("accumulator.sillytavern.ingestor")

LIMINAL_WORLD_KEY = "liminal"
PROVENANCE_VERSION = "provenance-v1"


class SillyTavernChatIngestor:
    """
    Ingest one SillyTavern JSONL chat as a provenance-preserving liminal
    conversation timeline. The active character context comes from the chat
    header; each message keeps its own speaker and viewpoint identity.
    """

    def __init__(self, db: Database):
        self.db = db

    async def ingest_file(self, path: Path) -> dict:
        parsed = parse_sillytavern_jsonl(path)
        source_id = f"sillytavern:{parsed.chat_key}"

        await self._ensure_source_identity(parsed, source_id)
        session_id = await self._get_or_create_session(parsed, source_id)

        timeline_id = await get_or_create_timeline(
            self.db,
            world_key=LIMINAL_WORLD_KEY,
            session_id=session_id,
            character_id=parsed.character_name,
            user_name=parsed.user_name,
            scope_key=f"source:sillytavern:{parsed.chat_key}",
            meta={
                "source_id": source_id,
                "source_kind": SOURCE_KIND,
                "accumulator_id": ACCUMULATOR_ID,
                "chat_key": parsed.chat_key,
                "file_name": parsed.path.name,
                "create_date": parsed.create_date,
                "world_assignment": "default_liminal",
                "provenance_version": PROVENANCE_VERSION,
            },
            source_id=source_id,
        )

        imported = 0
        parent_node_id = None
        for message in parsed.messages:
            event_id = await self._create_message_event(
                parsed,
                message,
                source_id=source_id,
                session_id=session_id,
            )

            recipient_id = (
                parsed.character_name
                if message.speaker_type == "user"
                else parsed.user_name
                if message.speaker_type == "character"
                else None
            )

            node_id, _ = await add_node_and_edge(
                self.db,
                timeline_id=timeline_id,
                event_id=event_id,
                character_id=parsed.character_name,
                kind="chat_message",
                speaker_id=message.speaker_id,
                speaker_role=message.speaker_type,
                recipient_id=recipient_id,
                message_text=message.text,
                payload=self._message_payload(parsed, message, source_id),
                viewpoint_id=message.viewpoint_id,
                parent_node_id=parent_node_id,
                edge_type="next",
            )
            parent_node_id = node_id
            imported += 1

        logger.info(
            "Imported SillyTavern chat %s character=%s user=%s messages=%d",
            parsed.chat_key,
            parsed.character_name,
            parsed.user_name,
            imported,
        )
        return {
            "chat_key": parsed.chat_key,
            "source_id": source_id,
            "session_id": str(session_id),
            "timeline_id": str(timeline_id),
            "character_name": parsed.character_name,
            "user_name": parsed.user_name,
            "messages": imported,
        }

    async def _ensure_source_identity(
        self,
        parsed: ParsedChatLog,
        source_id: str,
    ) -> None:
        existing = await self.db.fetchrow(
            "SELECT source_kind FROM aios.source_identity WHERE source_id=$1",
            source_id,
        )
        if existing and existing["source_kind"] != SOURCE_KIND:
            raise RuntimeError(
                f"source_id {source_id!r} already has source_kind "
                f"{existing['source_kind']!r}"
            )

        await self.db.execute(
            """
            INSERT INTO aios.source_identity (
                source_id, source_kind, display_name,
                canonical_uri, canonical_domain, meta
            )
            VALUES ($1,$2,$3,$4,NULL,$5::jsonb)
            ON CONFLICT (source_id) DO UPDATE
            SET display_name=COALESCE(aios.source_identity.display_name, EXCLUDED.display_name),
                canonical_uri=COALESCE(aios.source_identity.canonical_uri, EXCLUDED.canonical_uri),
                meta=aios.source_identity.meta || EXCLUDED.meta,
                updated_at=now()
            """,
            source_id,
            SOURCE_KIND,
            f"SillyTavern chat: {parsed.character_name}",
            f"sillytavern://chat/{parsed.chat_key}",
            json.dumps(
                {
                    "accumulator_id": ACCUMULATOR_ID,
                    "file_name": parsed.path.name,
                    "file_sha256": parsed.file_sha256,
                    "chat_key": parsed.chat_key,
                    "character_name": parsed.character_name,
                    "user_name": parsed.user_name,
                }
            ),
        )

    async def _get_or_create_session(
        self,
        parsed: ParsedChatLog,
        source_id: str,
    ) -> UUID:
        row = await self.db.fetchrow(
            """
            SELECT session_id
            FROM aios.session
            WHERE source='sillytavern_jsonl'
              AND source_session_id=$1
            ORDER BY created_at
            LIMIT 1
            """,
            parsed.chat_key,
        )
        if row:
            return row["session_id"]

        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.session (
                source, source_session_id, topic, meta
            )
            VALUES ('sillytavern_jsonl',$1,$2,$3::jsonb)
            RETURNING session_id
            """,
            parsed.chat_key,
            f"{parsed.character_name} / {parsed.user_name}",
            json.dumps(
                {
                    "source_id": source_id,
                    "accumulator_id": ACCUMULATOR_ID,
                    "file_name": parsed.path.name,
                    "file_sha256": parsed.file_sha256,
                    "create_date": parsed.create_date,
                    "character_name": parsed.character_name,
                    "user_name": parsed.user_name,
                    "chat_metadata": parsed.chat_metadata,
                }
            ),
        )
        return row["session_id"]

    async def _create_message_event(
        self,
        parsed: ParsedChatLog,
        message: ParsedMessage,
        *,
        source_id: str,
        session_id: UUID,
    ) -> int:
        message_sha = hashlib.sha256(
            message.text.encode("utf-8")
        ).hexdigest()
        source_event_id = (
            f"{parsed.chat_key}:message:{message.index}:"
            f"{message.speaker_type}:{message_sha}"
        )
        dedupe_key = f"sillytavern::{source_event_id}"

        recipient_id = (
            parsed.character_name
            if message.speaker_type == "user"
            else parsed.user_name
            if message.speaker_type == "character"
            else None
        )

        row = await self.db.execute_returning_row(
            """
            INSERT INTO aios.ingest_event (
                event_time, source, source_id, source_kind, source_event_id,
                kind, session_id, speaker_id, speaker_role, recipient_id,
                viewpoint_id, character_id, user_name, message_text, payload,
                dedupe_key, target_character_id, target_world_id,
                provenance_version
            )
            VALUES (
                COALESCE($1,now()),'sillytavern_jsonl',$2,$3,$4,
                'chat_message'::aios.event_kind,$5,$6,$7::aios.actor_type,$8,
                $9,$10,$11,$12,$13::jsonb,
                $14,NULL,NULL,$15
            )
            ON CONFLICT (dedupe_key) DO UPDATE
            SET dedupe_key=EXCLUDED.dedupe_key
            RETURNING event_id
            """,
            message.event_time,
            source_id,
            SOURCE_KIND,
            source_event_id,
            session_id,
            message.speaker_id,
            message.speaker_type,
            recipient_id,
            message.viewpoint_id,
            parsed.character_name,
            parsed.user_name,
            message.text,
            json.dumps(self._message_payload(parsed, message, source_id)),
            dedupe_key,
            PROVENANCE_VERSION,
        )
        return int(row["event_id"])

    def _message_payload(
        self,
        parsed: ParsedChatLog,
        message: ParsedMessage,
        source_id: str,
    ) -> dict:
        raw = message.raw
        swipes = raw.get("swipes")
        return {
            "text": message.text,
            "source_id": source_id,
            "source_kind": SOURCE_KIND,
            "accumulator_id": ACCUMULATOR_ID,
            "chat_key": parsed.chat_key,
            "file_name": parsed.path.name,
            "message_index": message.index,
            "character_id": parsed.character_name,
            "user_name": parsed.user_name,
            "speaker_id": message.speaker_id,
            "speaker_type": message.speaker_type,
            "viewpoint_id": message.viewpoint_id,
            "send_date_raw": raw.get("send_date"),
            "gen_started": raw.get("gen_started"),
            "gen_finished": raw.get("gen_finished"),
            "title": raw.get("title"),
            "extra": raw.get("extra") or {},
            "force_avatar": raw.get("force_avatar"),
            "swipe_id": raw.get("swipe_id"),
            "swipe_count": len(swipes) if isinstance(swipes, list) else 0,
            "swipes": swipes if isinstance(swipes, list) else [],
            "swipe_info": raw.get("swipe_info") or [],
            "selected_message_sha256": hashlib.sha256(
                message.text.encode("utf-8")
            ).hexdigest(),
            "identity_ruleset": "character-id-v1",
            "provenance_version": PROVENANCE_VERSION,
        }
