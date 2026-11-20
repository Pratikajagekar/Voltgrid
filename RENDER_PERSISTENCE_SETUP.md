# Keep VoltGrid data on Render

The ZIP uses SQLite by default for local development. Render's normal service
filesystem is temporary, so a database stored in the project folder can be reset
when the service is replaced or redeployed.

## Option A — simplest: persistent disk + SQLite

Use this for a single VoltGrid web service:

1. Open the VoltGrid Web Service in the Render Dashboard.
2. Add a Persistent Disk to the service.
3. Set its mount path to `/var/data`.
4. In Environment, add `RENDER_DISK_PATH` with value `/var/data`.
5. Deploy the service.
6. Open `/health`; it should return `status: ok` and `database: connected`.

On the first deployment with an empty disk, VoltGrid copies the bundled
`evcharge.db` and any included local upload files into the disk. On later
deployments it does not overwrite the database already on the disk.

Do not run multiple web-service instances against the same SQLite database.

## Option B — managed PostgreSQL + persistent uploads

1. Create a Render PostgreSQL database.
2. Set the web service's `DATABASE_URL` to the database's Internal URL.
3. Also attach a Persistent Disk mounted at `/var/data`.
4. Set `RENDER_DISK_PATH=/var/data` so payment proof and KYC uploads survive.
5. Deploy and verify `/health`.

PostgreSQL stores the relational records; the disk stores uploaded proof/KYC files.
The application creates missing tables and applies its light schema migrations.
It does not automatically import the SQLite file into an already-created PostgreSQL
database. Export/import and verify the old data before switching production traffic.

## Important data migration note

The archive includes a local `evcharge.db` and will seed a brand-new persistent
SQLite disk from it once. It cannot recover records that were already deleted from
Render's temporary filesystem, nor can it copy records that were never included
in this ZIP. Keep a backup of the current production database before changing the
database URL or storage configuration.
