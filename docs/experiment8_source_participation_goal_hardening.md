# Experiment 8: source ownership, participation readiness and goal review

Second pre-reset development pass (P4-P6). This change is additive to the
integrated belief/source migrations 10-13. It does not update AIOS1, wipe a
database, edit historical Experiment 7 evidence or promote Participation V4.

## P4: fidelity, testimony and source-bound retrieval

- Integrity V4 abstains on a source-local first-person subject resolved to an
  ungrounded different speaker, and on mixed third-/first-person frame objects
  (the Alex gym/phone Experiment 7 regression population).
- Source testimony remains evidence of what its speaker said; neither Integrity
  nor an `observed` acquisition label proves the underlying narrated event.
- HUD text renders inference/testimony origin as
  `reported/derived; underlying event not independently observed`, leaving
  stored acquisition status unchanged.
- Character retrieval selects provenance only from an active, currently
  source-eligible acquisition; episodic classification joins the actual
  acquisition's claim/observation pair, never a neighboring occurrence that
  happens to share a proposition.
- Do not introduce different ownership semantics for human users and characters:
  grammatical first person is bound to the source speaker identity.

## P5: opt-in participation evaluation

Migration `20261003_14_participation_worker_heartbeat.sql` establishes a
small operational heartbeat table. The independent worker registers BEFORE
printing its READY marker, refreshes its heartbeat after each batch and marks
it stale at shutdown. Enrollment requires a fresh heartbeat of the expected
shadow policy version; the API reports HTTP 503 if the evaluator is absent.
No worker is required for normal AIOS operation unless comparison enrollment
is explicitly requested.

Inspection reports expected/evaluated/pending/failed/skipped, heartbeat
readiness and a coverage state. Full receipt accounting is distinct from a
paired-live comparison: `complete_retrospective` is not equivalent to
`complete` / `paired_live`. Missing or stale Integrity V4 receipts cannot
contribute a `valid` representation-quality signal to the comparator.
V4 remains a SHADOW comparator, not the live memory authority.

Deployment opt-in:

```bash
export AIOS_PARTICIPATION_SHADOW_ENABLED=1
export AIOS_SEMANTIC_HYGIENE_APPLY_ENABLED=0
python -m aios_app.launch
```

Do not enroll until the optional participation service is marked READY.
The heartbeat checks actual DB liveness, not just the launch environment
variable. A stale/absent heartbeat means experiment enrollment fails visibly.

## P6: bounded goal lifecycle

- Goal reviews prioritize active goals with completion candidates or topic
  overlap, while still rotating older unreviewed goals through the budget.
- `planning.review` receives at most four already-scoped recent source turns
  (bounded per excerpt), including the character's prior response when available.
  The review prompt puts this evidence before auxiliary context rather than
  silently truncating it behind goal metadata.
- `planning.review` choices require strict source freshness at both creation
  and acceptance. Advancing the head invalidates an obsolete inference choice.
- Review option 2 only closes a goal if the operation is bound to the currently
  open attempt AND its evidence node follows the goal's original source
  occurrence on that attempt's source timeline. Same-origin reviews and late
  results from previous attempts cannot close current intentions.
- This does not claim that simple text overlap demonstrates fulfillment:
  planning.review still makes the bounded substantive completion choice.
  Terminal goals remain in history but are omitted from managed active goals
  and the HUD immediate-goal projection.

The original "tell Alex which sentence is weak" scenario should be used as an
integration fixture. Its fulfillment still requires later source evidence and
a review; source chronology alone does not automatically close arbitrary goals.

## Pre-reset validation

The fast semantic-topology CI job compiles the changed code and runs ownership,
HUD provenance, evaluator coverage and goal-lifecycle unit regressions. The
fresh-baseline smoke installs migration 14 and checks the readiness relation.
The disposable participation PostgreSQL test now seeds a test heartbeat before
enrolling an experiment.

Full AIOS1 PostgreSQL/Qdrant/Fuseki integration has NOT been performed by this
remote code patch. Finish end-to-end CI and remaining P7/P8 work before the
planned destructive development-database reset.
