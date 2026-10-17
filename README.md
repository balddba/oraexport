# OraExport

Schedule and manage Oracle Data Pump exports from a web interface. Export selected schemas or an entire PDB/non-CDB, monitor runs, and manage retained files. Dump and log files stay on the database server.

I created OraExport for my own use, to back up small projects that didn't require a full RMAN backup.

## Screenshots

Sample data shown below.

**Overview**

![Overview with active exports and recent runs](docs/screenshots/overview.png)

**Databases**

![Database registration and export targets](docs/screenshots/databases.png)

**Export tasks**

![Export tasks with schedules, retention, and manual runs](docs/screenshots/export-tasks.png)

**Task editor**

![Task editor with schema selection and scheduling options](docs/screenshots/task-editor.png)

**Exports**

![Export history and retained files](docs/screenshots/exports.png)

## Setup

You need Docker with Compose v2, an Oracle 19c+ metadata database, reachable Oracle target services with existing filesystem-backed DIRECTORY objects, and a TLS reverse proxy. Generating the encryption key below also requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

1. Create a dedicated metadata schema using your DBA account. Adjust the password, tablespace, and quota for your environment:

   ```sql
   CREATE USER oraexport_meta IDENTIFIED BY "<choose a strong password>"
     DEFAULT TABLESPACE users QUOTA 1G ON users;
   GRANT CREATE SESSION, CREATE TABLE TO oraexport_meta;
   ```

2. From the repository root, copy the environment template and generate an encryption key:

   ```bash
   cp .env.example .env
   uv sync --project backend --locked
   uv run --project backend python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

   Set these values in `.env`:

   | Setting | Value |
   | --- | --- |
   | `METADATA_URL` | Metadata connection, e.g. `oracle://oraexport_meta:password@oracle-host:1521/?service_name=APPMETA`. URL-encode special characters in credentials. |
   | `ENCRYPTION_KEY` | The generated key. Back it up separately; saved credentials require it. |
   | `ALLOWED_ORIGIN` | The exact public HTTPS origin, e.g. `https://exports.example.com`, with no trailing slash. |

   Use database addresses reachable from the containers and keep `COOKIE_SECURE=true` for HTTPS.

3. Start the application and create the first administrator:

   ```bash
   docker compose up --build -d --wait
   docker compose exec api oraexport bootstrap-admin --username admin
   ```

   Bootstrap prompts for a password of at least 12 characters. The API applies metadata migrations automatically on startup.

4. Point your TLS reverse proxy at `127.0.0.1:8080`, then open the public URL and sign in.

If startup fails, inspect `docker compose logs api worker frontend`.

## General usage

1. **Add a database.** In **Databases**, register the connection and DBA credentials, then select **Test & discover**. Add a direct target service for each PDB or non-CDB you want to export.
2. **Provision a target.** Select an existing Oracle DIRECTORY and permanent tablespace, review the generated SQL, and apply it.
3. **Create an export task.** Choose selected schemas or a full export, set the schedule and timezone, and choose export and retention options.
4. **Run and monitor.** Use **Run now** or wait for the schedule. Follow progress and recent results on **Overview**.
5. **Manage exports.** Open **Exports** or **Manage exports** on a target to inspect run history and retained files. Deleting an export's files keeps its execution history.
6. **Configure notifications and access.** Administrators can set up SMTP or an HTTPS webhook in **Settings** and add administrators, operators, or viewers.
