# aios_app/pipeline/reconstruct_document_test.py

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import List, Dict, Any
from uuid import UUID

from aios_app.db import Database
from aios_app.config import settings

logger = logging.getLogger("aios.pipeline.reconstruct_test")


# ============================================================
# Reconstruction modes
# ============================================================

MODES = {"chat", "document", "timeline"}


# ============================================================
# Helpers
# ============================================================

async def resolve_anchor(
    db: Database,
    section_id: UUID,
) -> Dict[str, Any]:
    """
    Resolve anchor section → dag_node context.
    """
    row = await db.fetchrow(
        """
        SELECT
          ds.section_id,
          ds.document_id,
          ds.section_path,
          dn.node_id,
          dn.timeline_id,
          dn.created_at
        FROM aios.document_section ds
        JOIN aios.dag_node dn ON dn.node_id = ds.node_id
        WHERE ds.section_id = $1
        """,
        section_id,
    )

    if not row:
        raise RuntimeError(f"section_id not found: {section_id}")

    return dict(row)


# ============================================================
# Reconstruction queries
# ============================================================

async def reconstruct_chat(
    db: Database,
    *,
    timeline_id: UUID,
) -> List[Dict[str, Any]]:
    return await db.fetch(
        """
        SELECT
          ds.section_id,
          ds.section_order,
          ds.section_path,
          ds.content
        FROM aios.document_section ds
        JOIN aios.dag_node dn ON dn.node_id = ds.node_id
        WHERE dn.timeline_id = $1
          AND ds.section_path LIKE '/chat/%'
        ORDER BY ds.section_order
        """,
        timeline_id,
    )


async def reconstruct_document(
    db: Database,
    *,
    document_id: UUID,
) -> List[Dict[str, Any]]:
    return await db.fetch(
        """
        SELECT
          section_id,
          section_order,
          section_path,
          content
        FROM aios.document_section
        WHERE document_id = $1
          AND section_path LIKE '/paragraph/%'
        ORDER BY section_order
        """,
        document_id,
    )


async def reconstruct_timeline(
    db: Database,
    *,
    timeline_id: UUID,
) -> List[Dict[str, Any]]:
    return await db.fetch(
        """
        SELECT
          ds.section_id,
          ds.section_order,
          ds.section_path,
          ds.content,
          dn.created_at
        FROM aios.document_section ds
        JOIN aios.dag_node dn ON dn.node_id = ds.node_id
        WHERE dn.timeline_id = $1
        ORDER BY dn.created_at
        """,
        timeline_id,
    )


# ============================================================
# Main entry
# ============================================================

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconstruct a document/chat/timeline from document_section"
    )
    parser.add_argument(
        "section_id",
        type=UUID,
        help="Anchor section_id to reconstruct from",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(MODES),
        default="auto",
        help="Reconstruction mode (chat | document | timeline | auto)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug view",
    )

    args = parser.parse_args()

    db = Database(settings.db_dsn)
    await db.connect()

    try:
        anchor = await resolve_anchor(db, args.section_id)

        # --------------------------------------------------
        # Auto-detect mode if requested
        # --------------------------------------------------
        mode = args.mode
        if mode == "auto":
            path = anchor["section_path"]
            if path.startswith("/chat/"):
                mode = "chat"
            elif path.startswith("/paragraph/"):
                mode = "document"
            else:
                mode = "timeline"

        print("\n=== RECONSTRUCTED DOCUMENT ===\n")
        print(f"[MODE: {mode}]\n")

        # --------------------------------------------------
        # Dispatch
        # --------------------------------------------------
        if mode == "chat":
            rows = await reconstruct_chat(
                db,
                timeline_id=anchor["timeline_id"],
            )
        elif mode == "document":
            if not anchor["document_id"]:
                raise RuntimeError("Anchor section has no document_id")
            rows = await reconstruct_document(
                db,
                document_id=anchor["document_id"],
            )
        elif mode == "timeline":
            rows = await reconstruct_timeline(
                db,
                timeline_id=anchor["timeline_id"],
            )
        else:
            raise RuntimeError(f"Unknown mode: {mode}")

        # --------------------------------------------------
        # Output reconstructed content
        # --------------------------------------------------
        for r in rows:
            text = r["content"]
            if text:
                print(text.strip())
                print()

        # --------------------------------------------------
        # Debug view
        # --------------------------------------------------
        if args.debug:
            print("\n=== DEBUG VIEW ===\n")
            print(f"MODE: {mode}")
            print(f"ANCHOR_SECTION: {args.section_id}\n")

            for r in rows:
                print(
                    f"- section_id={r['section_id']} "
                    f"order={r.get('section_order')} "
                    f"path={r.get('section_path')}"
                )

    finally:
        await db.close()


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
