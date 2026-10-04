# AIOS developer documentation

Start with a running server, complete one interaction, then inspect its state.
These guides describe the development API; pin the AIOS commit used by your
application. AIOS remains experimental.

| Goal | Guide |
|---|---|
| Install and start AIOS | [Installation](installation.md) |
| Connect your application | [First integration](integration.md) |
| Look up requests, responses, and errors | [API guide](api.md) |
| Operate services and configure inference | [Runtime operations](runtime.md) |
| Find a message, missing memory, or failed job | [Inspection and troubleshooting](inspection.md) |
| Understand IDs, state, and storage | [Architecture](architecture.md) |
| Understand accepted character identity | [Identity kernel](identity_kernel.md) |
| Inspect belief admission | [Belief reconciliation](belief_reconciliation.md) |
| Run a participation experiment | [Participation experiment](participation_experiment.md) |

The Python [example client](../examples/live_client.py) uses only the standard
library. It talks to AIOS over HTTP; your application does not need the AIOS
Python package or direct database access.

## First-pass validation boundary

Examples are checked against route definitions and request models. The Python
client has also been exercised against a local mock HTTP service for readiness
retries, reply ingestion, stale-snapshot rejection, and conflict propagation;
Markdown links and Bash snippet syntax have been checked. A complete
fresh-install and live ingestion/HUD run remains a release acceptance check.
Do not interpret illustrative responses as captured live results.

Before tagging a release, run the tutorial on a clean host, export that server's
OpenAPI schema, verify repeat submission and restart/resume behavior, and check
that the inspection queries work against the fully migrated database. Record
that release's commit and supported environment. Backup/restore across all
three stores also needs a separately verified operator procedure.
