# AIOS HUD Plugins

This package is the runtime extension boundary for HUD and retrieval context.

Plugins contribute ephemeral state to an active AIOS runtime instance. Plugin
state does **not** become a DAG event, RDF proposition, character memory, or
world truth unless another explicit ingestion path promotes it.

## Adding a provider

Place provider modules under `aios_app/plugins/providers/`. A discoverable
module exposes a module-level `PLUGIN` object implementing `AIOSPlugin`.

A plugin may contribute:

- namespaced state for JSON clients,
- typed HUD sections and fields,
- retrieval signals that bias existing HUD relevance/topology retrieval,
- action names for the current runtime surface.

Plugins should use `applies_to()` to restrict themselves to relevant
instances, characters, entities, or worlds.

## Retrieval

Only normalized retrieval signals affect retrieval. A plugin can supply them
explicitly with `RetrievalSignal`, or set a HUD field's `retrieval_role` to
something other than `none`. AIOS converts those values into focus text before
the normal branch-aware relevance and topology retrieval stages. Plugins never
receive direct authority to retrieve arbitrary graph data.

## Reliability

Plugin collection runs concurrently. Each plugin has a short timeout and a
failure or timeout degrades to plugin status metadata instead of blocking the
HUD. Contributions may also set `observed_at` and `ttl_seconds`; stale
snapshots are discarded.

`startup()` and `shutdown()` run with the FastAPI application lifecycle.

## Built-in demo

`builtin/demo_status.py` exercises discovery and rendering but is disabled by
default. Set `AIOS_ENABLE_DEMO_HUD_PLUGIN=1` to enable it.
