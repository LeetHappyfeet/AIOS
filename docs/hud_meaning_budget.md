# HUD source meaning and rendered budgets

Message cognition V13 preserves negative statements whose clause scope cannot
be safely represented by the fast parser. These units have
`text_representation=source_statement`, the full original sentence as `text`
and `object`, and `predicate=source_statement`. Their positive polarity asserts
the *whole source statement*, including its negative words; it does not assert
the old affirmative SPO extraction. Simple single-clause contracted negative
states retain their existing negative polarity and explicit negative wording.
The provisional bridge retains representation and source text. For legacy
negative rows, the HUD uses the source when available; otherwise it labels the
scope unavailable rather than silently presenting affirmative wording.

Goal source admission V3 additionally defers indefinite outcomes, abstract
desired states, unresolved object pronouns regardless of following adverbs,
incomplete objectives, unresolved communication topics and recognized figurative
objectives. Markdown emphasis does not bypass review. Rejected candidates stay
in source-linked ingestion diagnostics and the original DAG message; no new
inference call or guessed antecedent is introduced. The same guard protects
direct source reconciliation and model-formulated goal admission. Concrete
projects, learning goals and specified future communication remain eligible.
This is a conservative lexical gate, not a complete idiom or reference resolver.

Canonical assembled V6 frames expose `hud.token_budget` and section budgets.
The HUD version bump invalidates previously prepared frames.
The text renderer now enforces those budgets *after* rendering labels, nested
plans, provenance and worker results. It uses the existing estimate of four
characters per token; this bounds rendered characters, not exact model tokens.
It reserves the latest available user source message ahead of identity/history,
uses marked head/tail excerpts for long source messages, and omits complete
optional records rather than chopping claims or action contracts. Source
headings and branch-boundary guidance are retained when space allows. Zero and
very small budgets remain bounded. Unbudgeted compatibility frames retain their
existing full rendering.

Budgeted worker results show short structured summaries, limitations and check
scope before findings. Full reports and requested content remain in the
existing retained-result retrieval path. Neither the canonical frame nor stored
source/result data is mutated by rendered budgeting.
Retrieved pages retain complete JSON envelopes. If a page must shrink, the HUD
delivers a text prefix with `hud_truncated=true` and advances `next_cursor` only
over the visible characters, retaining the original report revision.

This update does not cancel existing managed goals or rewrite historical
knowledge. The existing opt-in goal revalidation command still repairs only its
previously supported narrow cases. Other historical ambiguities require a
source-backed lifecycle review. UUID aliases, identity facet migration and recall
quality scoring are separate follow-ups.

Regression checks: `python -m pytest -q tests/test_hud_meaning_budget.py`
