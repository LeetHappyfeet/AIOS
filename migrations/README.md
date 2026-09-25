# AIOS migrations

AIOS has a single supported database line.

- `../aios_baseline.sql` is the immutable canonical fresh-install PostgreSQL schema.
- `current/` contains the only migrations executed by `aios_app.migrate`.
- Pre-baseline prototype migrations were removed from the working tree after the baseline became authoritative; Git history remains the archive.

A database created before the baseline is deliberately not auto-upgraded by the current branch. Create a fresh database and let `python -m aios_app.migrate` initialize it from the baseline instead.

For every future schema change, add one immutable migration to `current/`. Do not edit an already-applied migration or the released baseline.
