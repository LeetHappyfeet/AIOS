# AIOS migrations

AIOS now has a single supported database line.

- `../aios_baseline.sql` is the canonical fresh-install PostgreSQL schema.
- `current/` contains the only migrations executed by `aios_app.migrate`.
- SQL files directly in this directory and files under `legacy/` are prototype-era history. They are not executed by the current migrator and exist only for historical inspection until they are removed from the development tree.

A database created before the baseline is deliberately not auto-upgraded by the current branch. Create a fresh database and let `python -m aios_app.migrate` initialize it from the baseline instead.

For every future schema change, add one immutable migration to `current/`. Do not edit an already-applied migration or the released baseline.
