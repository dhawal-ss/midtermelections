# Migrations

Schema changes after the base `sql/schema.sql` go here as `NNN_description.sql` (e.g. `001_add_rcv_rounds.sql`).
`python -m scripts.initialize_db` applies each file once and records it in `schema_migrations`.
