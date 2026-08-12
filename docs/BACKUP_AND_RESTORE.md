# Backup and restore

Toda's SQLite database and workspace tree are both required for a complete
backup. Connector credentials held by external providers must be backed up or
reconnected according to those providers' policies.

Stop Toda or use SQLite's online backup mechanism before copying the database;
never copy a live database file and assume it is consistent. Back up the whole
`/var/lib/toda` volume, encrypt the archive, restrict access, and regularly test
restoration into an isolated installation.

For Compose, stop the service before a filesystem-level volume snapshot:

```bash
docker compose stop toda
# Snapshot or archive the toda-data volume using your platform's supported tool.
docker compose start toda
```

Restoration is successful only after `/health` passes and representative
Sessions, Tasks, memories, artifacts, and workspaces can be inspected.
