# Active AIOS migrations

`aios_baseline.sql` is the canonical PostgreSQL schema for a fresh AIOS install.

Only new migrations created after that baseline belong in this directory. Migration files are applied in lexical filename order and are immutable once released.

The SQL files in the parent `migrations/` directory and `migrations/legacy/` are pre-baseline development history. They are retained for archaeology and source history only; the active migrator does not execute them.

Rules for new migrations:

1. Never edit `aios_baseline.sql` to ship an incremental schema change.
2. Add a new `.sql` file here instead.
3. Use a sortable timestamp/name prefix, for example `20260914_add_example.sql`.
4. Never modify an already-applied migration; add a later migration.
5. A fresh database must always reach the same schema as an upgraded database at the same revision.
