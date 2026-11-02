# Runtime operations

Use [Installation](installation.md) for first setup. Commands below run from
the repository root unless stated otherwise.

## Start, verify, and stop

```bash
bash run.sh
```

Wait for `AIOS READY` and `Required services: 4/4 ready`. The required services
are Accumulator, Supervisor, Pipeline Runner, and API. UI, Semantic Index, and
Semantic Topology have separate readiness reporting; required-service readiness
alone does not prove all optional services are available.

In a second terminal:

```bash
curl --fail-with-body -sS http://127.0.0.1:8000/healthz
curl --fail-with-body -sS http://127.0.0.1:8000/agent/runtime/versions
docker compose ps
```

`/healthz` returns `{"ok":true}` from a simple handler. It is an API reachability
check, not a full pipeline, semantic-integrity, or three-store health audit.
Use the launcher, manifest, and [inspection guide](inspection.md) together.

Press Ctrl+C in the launcher terminal to stop native processes. Then run
`bash stop.sh` to stop Compose infrastructure while preserving named volumes.
Do not run another launcher over the same active installation.

## Configuration

Workspace client credentials can be saved in Gradio → **Workspace Access**.
The installation-local `launch-config.json` persists the key across launches;
see [workspace setup](workspace-client.md) for copying, environment precedence,
and the optional `AIOS_LAUNCH_CONFIG` path override.

[.env.example](../.env.example) lists infrastructure settings. Python settings
are defined in [config.py](../config.py); launcher-specific controls are in
[launch.py](../launch.py). Keep database credentials and service URLs consistent
with Compose settings. Exporting variables in the launch shell makes the
configuration passed to child processes explicit:

```bash
export AIOS_API_HOST=127.0.0.1
export AIOS_DEFAULT_TIMEZONE=America/New_York
AIOS_LOG_MODE=verbose bash run.sh
```

| Setting | Current default / purpose |
|---|---|
| `AIOS_DB_DSN` | `postgresql://postgres:postgres@127.0.0.1:5432/postgres` |
| `AIOS_FUSEKI_BASE_URL` | `http://127.0.0.1:3030` |
| `AIOS_QDRANT_URL` | `http://127.0.0.1:6333` |
| `AIOS_API_HOST`, `AIOS_API_PORT` | `0.0.0.0`, `8000`; use loopback for local-only access |
| `AIOS_DEFAULT_TIMEZONE` | Unset; narrative relative-time interpretation |
| `AIOS_LOG_MODE` | `normal`; also `verbose` or `debug` |
| `AIOS_CORE_STARTUP_TIMEOUT` | 60 seconds for core startup readiness |
| `AIOS_SEMANTIC_INDEX_STARTUP_TIMEOUT` | 120 seconds for index startup |
| `AIOS_PARTICIPATION_SHADOW_ENABLED` | Disabled; explicitly enables the separate shadow worker |

Use current `AIOS_RUNNER_*_WORKERS` settings in `config.py` when tuning pools.
Begin with defaults and inspect queue age, resource class, and host pressure
before increasing concurrency. More workers can compete for the same CPU,
connections, or RDF service.

## Logs and infrastructure

```bash
docker compose logs --tail=100 postgres fuseki qdrant
docker compose logs fuseki-init
docker compose logs -f qdrant
```

These are infrastructure logs. Native API/supervisor/runner output appears in
the launcher terminal. For a diagnostic capture, start the stopped application
with:

```bash
set -o pipefail
AIOS_LOG_MODE=verbose bash run.sh 2>&1 | tee aios-runtime.log
```

Include the failing service/job, timestamps, AIOS commit, and relevant source
coordinates when reporting problems. Logs and HUDs can contain source text;
review a diagnostic capture before sharing it.

## Optional inference providers

The client tutorial uses your application's model. AIOS can separately route
internal cognitive work to an OpenAI-compatible provider. Inspect configured
providers with:

```bash
curl --fail-with-body -sS http://127.0.0.1:8000/inference/providers
```

Create/update providers with `POST /inference/providers`. The request requires
`provider_key`, `display_name`, `base_url`, and `model`; the schema also exposes
`api_key_env`, concurrency, timeout, worker classes, and capabilities. Use the
actual endpoint and model ID advertised by your provider. `api_key_env` names
an environment variable on the AIOS host, not the secret itself. Export that
variable before launching AIOS.

Use `POST /inference/providers/{provider_id}/health` to check a configured
provider, and `PATCH /inference/providers/{provider_id}` for enabled/drain
controls. Do not declare JSON-mode or other capabilities until the actual model
and serving endpoint support the required responses. Provider readiness alone
does not prove a particular cognitive task has succeeded.

## Upgrade and recovery boundary

Stop native writers, record `git rev-parse HEAD`, and back up the installation
before upgrading. Update to the intended release, refresh dependencies through
`setup.sh`, and start through `run.sh`; the launcher owns migrations and checks.
Do not edit applied migration files to resolve a checksum/ledger mismatch.
Use the diagnostics in [the pre-reset release gate](experiment8_pre_reset_release_gate.md).

Data lives in PostgreSQL, Fuseki, and Qdrant. A PostgreSQL dump alone is not a
complete installation backup. Quiesce application writes and coordinate backups
of all three stores, configuration, and code version. This first pass does not
yet provide a validated cross-store restore recipe. Test restoration into an
isolated installation before relying on a backup procedure.

The installation guide identifies destructive volume removal. A production
recovery attempt should not begin with deleting volumes or resetting databases.


### Background RDF recall deadlines

`AIOS_RDF_INQUIRY_TIMEOUT_SECONDS` defaults to **5 seconds per Fuseki query**,
clamped to 0.1–30 seconds. Set it in the installation's `.env` and restart AIOS
to change it, for example `AIOS_RDF_INQUIRY_TIMEOUT_SECONDS=10`.
Background character inquiries use this timeout for both source and atom reads;
interactive HUD recall retains its 0.8-second transport limit. Each HTTP call
has zero retries and an async deadline of the selected timeout plus 0.1 seconds.
A recall can run both source and atom queries, each with its own deadline.
The SQL budgets are separate and unchanged. Traces/logs report the selected
transport budget, so a SQL timeout is distinguishable from an HTTP read timeout.
`AIOS_FUSEKI_TIMEOUT` does not override this dedicated recall budget.

An expired pipeline lease after shutdown is recovered at runner startup; while
running, the recovery loop checks at most every 60 seconds with default settings.
Recovery requeues the pipeline job. A goal-planning handler whose cognitive
operation already succeeded returns without another inference, allowing the
pipeline job to finish. No manual status edits are needed for that case.
A job that remains expired while the runner is active requires investigating
runner liveness and lease-recovery errors; an expired row alone cannot establish
that recovery failed if the runner was stopped.

### Blocked normalization revisions

Migration `20261030_01_normalization_blocked_revisions.sql` adds
`aios.claim_normalization_state`. Stop all AIOS workers, pull the development
branch, run `python -m aios_app.migrate` from the package's parent directory,
and restart. Existing claims get one attempt under the new policy; no source,
frame, observation or character memory is deleted or reingested.

If integrity validation and deterministic repair cannot produce a valid claim,
the normalizer records `outcome='blocked'`, its input revision, integrity reasons,
and normalizer/validator/repair versions. The supervisor skips that exact blocked
revision. Duplicate jobs already queued also skip validation for a current block.
The pipeline job still finishes, while its normalization outcome explicitly
records the lack of semantic progress. Validity requirements remain unchanged.

Source, sentence, section, DAG origin, semantic frame/projection and resolved
claim-context changes advance the input revision using database triggers. Pure
processing timestamps and integrity receipt writes do not. New revisions and
normalizer/validator/repair version changes allow another attempt and reopen the
bounded deterministic repair allowance. Bump the corresponding version constant
when changing one of these algorithms. Deterministic repair that changes a frame
also advances the revision; a still-blocked result may need one further pass to
record the stable repaired revision.

Receipt writes compare the attempted revision with the current input revision.
An edit arriving during validation cannot stamp a block onto a newer revision.
The old attempt remains diagnostic history until replaced by the next outcome.
This removes normalization churn; expensive vector/topology SQL may still need
independent profiling after the redundant work subsides.

Inspect current blocks:

```sql
SELECT integrity_status,reason_codes,count(*) AS claims
FROM aios.claim_normalization_state
WHERE outcome='blocked' AND attempted_revision=input_revision
GROUP BY integrity_status,reason_codes
ORDER BY claims DESC;
```
