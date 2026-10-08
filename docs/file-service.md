# File storage

`app/storage.py` selects a content adapter through `FILE_STORAGE`. File metadata
stays in PostgreSQL and includes `user_id`, `storage` and `storage_key`. The
backend checks ownership for upload, attachment reads, downloads and deletion;
clients receive safe file metadata without provider credentials.

| Backend | Configuration |
| --- | --- |
| `local` | `ASSISTANT_FILE_ROOT`; Compose mounts the persistent `filesdata` volume |
| `cos` | `COS_SECRET_ID`, `COS_SECRET_KEY`, `COS_REGION`, `COS_BUCKET`; optional `COS_PREFIX` |
| `fileservice` | Example plugin `plugins_examples.fileservice`, not loaded unless `LUMA_PLUGINS` names it. Then requires HTTPS `FILE_SERVICE_URL`, `FILE_SERVICE_APP_KEY`, `FILE_SERVICE_APP_SECRET`; optional folder ID and request timeout |

`local` and `cos` are the built-in backends. `COS_REGION` is required when
`FILE_STORAGE=cos` and has no default region. This release does not contain an
S3-compatible adapter. The file-service plugin
can connect to a separately operated HTTPS service that implements the existing
`/api/fs/` contract. Its requests use application credentials and timestamped
HMAC signatures. Keep `FILE_SERVICE_ALLOW_HTTP=false` for deployments.

Uploads use bounded streams and private staging directories. Set
`ASSISTANT_MAX_UPLOAD_BYTES` for the server limit. Soft-deleted content is swept
after `FILE_PURGE_AFTER_HOURS`; delete operations are idempotent, and remote
content requests occur outside database transactions. Metadata is removed after
content deletion succeeds.

Changing the default backend affects new uploads. Existing rows keep using
their recorded adapter, so keep the relevant credentials available until old
files are migrated or deleted. Local staging and sandbox snapshots still need a
persistent volume when the main storage backend is remote.

The Docker backup script saves PostgreSQL and the entire local data volume.
Remote COS/file-service content requires a separate provider backup. Keep the
Fernet key used for encrypted credentials alongside protected deployment
configuration, independently of content backups.
