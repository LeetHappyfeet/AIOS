"""Read-only plans for the exact RDF recall SQL, with no Fuseki or inference.

python -m aios_app.entity_memory.diagnostics --instance-id UUID --query 'cookbook' --analyze
The default is EXPLAIN only; --analyze executes the SELECTs to measure them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from uuid import UUID

from aios_app.entity_memory.retrieval import _SOURCE_SQL, _ATOM_SQL, owners_for, terms_for


async def query_plans(db, context, query: str, *, analyze: bool = False,
                      timeout_seconds: float = 30.0) -> dict:
    """Bound the entire diagnostic and prohibit writes, even during ANALYZE."""
    timeout_seconds = max(.01, min(float(timeout_seconds), 120.0))
    terms = terms_for(query, context.character_id)
    report = {"instance_id": str(context.instance_id), "terms": terms,
              "analyze": analyze, "timeout_seconds": timeout_seconds, "plans": {}}
    args = (getattr(context, 'source_head_node_id', None),
            getattr(context, 'source_timeline_id', None), context.character_id,
            owners_for(context), [], terms)
    async def inspect():
        async with db.atomic():
            await db.execute('SET TRANSACTION READ ONLY')
            await db.fetchval("SELECT set_config('statement_timeout',$1,true)",
                              f'{int(timeout_seconds*1000)}ms')
            await db.fetchval("SELECT set_config('lock_timeout',$1,true)", '1000ms')
            report['server_version'] = await db.fetchval("SELECT current_setting('server_version')")
            report['indexes'] = [dict(row) for row in await db.fetch(
                """SELECT tablename,indexname,indexdef FROM pg_indexes
                   WHERE schemaname='aios' AND tablename=ANY($1::text[])
                   ORDER BY tablename,indexname""", [
                    'entity_memory_projection','dag_edge','dag_node','document_section',
                    'knowledge_acquisition_event','proposition','epistemic_authority_admission',
                    'semantic_evidence_admission','extracted_sentence','claim_candidate'])]
            report['table_estimates'] = [dict(row) for row in await db.fetch(
                """SELECT relname,reltuples::bigint AS estimated_rows
                   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname='aios' AND relname=ANY($1::text[])
                   ORDER BY relname""", ['entity_memory_projection','dag_edge',
                    'knowledge_acquisition_event','proposition'])]
            options = 'ANALYZE, BUFFERS, SETTINGS, FORMAT JSON' if analyze else 'FORMAT JSON'
            for stage, sql in (('sources', _SOURCE_SQL), ('atoms', _ATOM_SQL)):
                started = time.monotonic()
                try:
                    # A failed EXPLAIN must not prevent the second query plan.
                    async with db.atomic():
                        rows = await db.fetch(f'EXPLAIN ({options}) ' + sql, *args)
                    plan = rows[0]['QUERY PLAN'] if rows else []
                    if isinstance(plan, str):
                        plan = json.loads(plan)
                    report['plans'][stage] = {'elapsed_ms': round((time.monotonic()-started)*1000,1),
                                             'explain': plan}
                except Exception as exc:
                    report['plans'][stage] = {'elapsed_ms': round((time.monotonic()-started)*1000,1),
                                             'error': type(exc).__name__, 'detail': str(exc)[:500]}
    try:
        await asyncio.wait_for(inspect(), timeout_seconds)
    except asyncio.TimeoutError:
        report['timed_out'] = True
    return report


async def run(args):
    from aios_app.config import settings
    from aios_app.db import Database
    from aios_app.hud.context import HUDContextResolver
    db = Database(settings.db_dsn)
    await db.connect()
    try:
        # Resolving coordinates is read-only and does not create an instance.
        context = await asyncio.wait_for(HUDContextResolver(db).resolve(args.instance_id), args.timeout)
        result = await query_plans(db, context, args.query,
                                   analyze=args.analyze, timeout_seconds=args.timeout)
        print(json.dumps(result, indent=2, default=str))
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-id', type=UUID, required=True)
    parser.add_argument('--query', required=True)
    parser.add_argument('--analyze', action='store_true', help='Execute the read-only SELECTs and include actual timings/buffers')
    parser.add_argument('--timeout', type=float, default=30.0, help='Overall query-plan budget in seconds (maximum 120)')
    args = parser.parse_args()
    if not 0 < args.timeout <= 120:
        parser.error('--timeout must be greater than zero and at most 120 seconds')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
