# HUD P2: recall, plans, identity and references

HUD V7 invalidates older prepared frames. Provisional message cognition now
passes structure, quality, relevance-floor and total-item-cap checks; it still
fills gaps after established recall and bypasses only current-source duplicate
suppression. Speech fragments such as `Shego / say / a voice` are suppressed
with a diagnostic reason. Incidental tone/voice/posture descriptions receive a
penalty when that facet is outside the current focus. Opposite polarities and
distinct source-scoped statements are not collapsed by lexical deduplication.
Stored memories are not deleted or rewritten.

The provisional bridge separates parser confidence from confidence in the
reported content, using source metadata and conservative legacy defaults.
Both are labelled in the HUD; profile settings can hide both confidence values.

Stale advisory plans are withheld instead of printing obsolete steps and open
questions. Goal knowledge lines name the actual prerequisite. Worker report
timestamps appear when available. Planning instructions carry reported findings
and limitations forward without requiring repeated inspection solely because
they are worker reports. Missing, contradictory, outdated or out-of-scope facts
can still justify a fresh check. This does not independently verify a report or
prove that a planning model will obey every instruction.
The planning contract version changes to V4: old-contract plans are withheld
and their continuations cannot proceed under the new contract. The next normal
planning review can replace them; rendering alone does not trigger inference.

New character-card descriptions use a general description facet. Identity
compiler V2 labels legacy `appearance.description` neutrally in presentation;
original stored facets and revision history stay intact. A newer explicit
description takes presentation precedence. Budgeted identity projections share
space between authored facets, including personality and constraints, rather
than spending it all on a long description. No generated personality summary or
new inference call is introduced.

Templates resolve at delivery, not in the shared identity cache. `{{char}}` uses
the character name, `{{user}}` uses the latest available user source's speaker,
and `{{isodate}}` uses the UTC delivery date. Idle duration is unknown unless
explicit HUD context supplies it. Unsupported/unavailable values become unknown;
the renderer does not guess them or copy a different user's cached value.

Prompt identifiers use lossless base64url encodings of the complete 128-bit UUID:
`g_` for goals, `r_` for runs, and `q_` for requests. They are 24
characters, never truncated UUIDs or mutable aliases. Token savings depend on
the model tokenizer. Work admission and result retrieval accept both original
UUIDs and the appropriate short reference, normalize before hashing/lookup, and
retain the existing instance, source-ancestry, active-goal and open-attempt
checks. Wrong reference types and noncanonical encodings are rejected. A stale
reference cannot be reassigned to a different goal; retrying the same request
retains its original canonical identity and idempotence.

Canonical API response/frame IDs remain UUIDs. Shortening affects HUD text,
including retrieved-page reference fields; report text and pagination offsets
are unchanged. Instance/world header coordinates remain canonical so operators
can still copy them into existing API/UI inputs. No database migration, client update or historical-goal cleanup
is required by this pass.
