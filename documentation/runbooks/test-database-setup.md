# Test database setup

What a clean PostgreSQL instance needs before the backend test suite (`cd backend && uv run
pytest -q`) runs green against it. Reach for this the first time you set up the backend locally,
or whenever the suite fails at collection with a database-name or permission error before a single
test has run.

The suite enforces two things itself, on every run, before any test executes: the configured
database must be recognisably a test database, and a specific cross-database permission wall must
already be in place. Both are set up out of band, once, by the steps below — nothing in the
codebase creates them for you.

## Prerequisites

**PostgreSQL 18.** The first migration asserts that the server has native `uuidv7()` support and
fails immediately, at migration time, on a server that predates it — better that than a silent
wrong ID scheme the first time a row is inserted. Confirm your server's major version before going
further.

**A role with administrative reach on your PostgreSQL instance**, at least for the setup steps
below — creating databases, roles, and revoking default privileges is a one-time, out-of-band
setup, not something the application's own runtime role needs to be able to do.

## 1. Create the control-plane database and its role

The application connects to one database for its own control-plane data — projects, chats, the
app registry, and so on. Create it and a login role for the application to connect as:

```sql
CREATE ROLE <app_role> LOGIN PASSWORD '<a password>';
CREATE DATABASE <your_test_db> OWNER <app_role>;
```

Run these as a superuser. Since PostgreSQL 16, creating a database owned by *another* role
requires the creating role to be a member of that role — the right to create databases is not
enough on its own, and the failure names the role rather than the privilege, which reads as a
configuration error rather than a missing grant. A role that simply creates the database for
itself, without the `OWNER` clause, sidesteps this.

**The database name must contain the substring `test`.** `backend/tests/conftest.py` reads the
configured connection string at collection time — before any test runs — and raises immediately
if the database name doesn't contain it. That guard exists so a missing or misconfigured local
config file fails loudly instead of quietly running the suite against whatever database happens to
be configured, development or otherwise. `citizen_one_test` is the name already used elsewhere in
this repository's own tooling; reusing it is not required, but the substring is.

The first migration also creates the `vector` extension. On a stock instance this needs a
superuser — owning the database is not sufficient, because the extension is not a trusted one — so
create it once, as a superuser, in the database you just made:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

If this is skipped the failure arrives at the first migration rather than during the suite, naming
the extension and the privilege it needed.

## 2. Revoke PUBLIC's default CONNECT on the control-plane database

PostgreSQL grants every role `CONNECT` on a newly created database by default, including a
database it does not own. Left in place, that means an application's own per-project database
role — which is deliberately walled off from every other project's data — could still open a
session against the control-plane database and read its catalog as reconnaissance. Closing that
door on the control-plane database itself is not something the per-project provisioning code can
do; it is a step this runbook exists to make sure happens once, out of band, when the cluster is
stood up:

```sql
REVOKE CONNECT ON DATABASE <your_test_db> FROM PUBLIC;
GRANT CONNECT ON DATABASE <your_test_db> TO <app_role>;
```

This is not optional decoration. A per-project-database test asserts directly that a freshly
provisioned application role is refused a connection to the control-plane database, and it fails
loudly — with a permission error, not a skip — on any cluster where this step was skipped. If you
see `tests/services/appdb/` failures naming a permission error against the control-plane database,
this is the step to check first.

## 3. Create the maintenance role for per-project databases

Every generated application gets its own PostgreSQL database and role, each created and later torn
down through a dedicated maintenance identity — never through the control-plane role above, and
never pointed at the control-plane database. Create it and point it at a neutral maintenance
database (`postgres`, not your test database):

```sql
CREATE ROLE bial_appdb_maint LOGIN CREATEDB CREATEROLE NOSUPERUSER PASSWORD '<a password>';
```

This role does **not** need to be a PostgreSQL superuser for its ordinary work. The provisioning
code that runs as this role explicitly grants itself membership in every per-project role it
creates, which is what later lets it terminate that role's connections and drop what it owns,
without any elevated privilege. It needs broader privilege only to act on objects it does *not*
own: a database or role left behind by an earlier, interrupted run. A freshly created cluster has
no such debris, so this role is enough to start from a green suite.

If the per-project-database teardown tests (`tests/services/appdb/test_teardown.py`,
`tests/api/v1/projects/test_delete_teardown.py`, `tests/api/v1/admin/test_disable_severs_db.py`)
begin failing intermittently — and the *set* of failing tests changes between otherwise identical
runs — suspect accumulated leftover per-project databases and roles from earlier interrupted runs
before suspecting a code change. Clearing that debris (or, as a last resort, running the affected
tests as an actual superuser) is the fix; re-reading a diff for a failure that moves between
identical runs will not find one.

This suite creates and drops **real** databases and roles on your PostgreSQL instance as part of
the ordinary (non-opt-in) test run — it is not gated behind an integration marker.

## 4. Generate the app-database encryption key

Every per-project database role's password is stored encrypted at rest. Generate a throwaway key
for local use:

```sh
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 5. Fill in your local test configuration

Copy `backend/.env.test.example` to `backend/.env.test` and fill in every value it documents —
the file is the authoritative template, including which fields can safely stay as the dummy values
already shown (Entra ID and superadmin-allowlist values are not exercised against a live tenant in
this suite) and which must point at what you built above: the control-plane connection string from
step 1, the maintenance connection string from step 3, and the encryption key from step 4.

## 6. Run migrations, including after merging `main`

```sh
cd backend && uv run alembic upgrade head
```

Run this again every time you merge or rebase onto `main`: a teammate's change may have added a
migration your database doesn't have yet, and the suite runs against whatever schema is actually
there — not the one the model definitions describe. A stale schema surfaces as test failures that
look unrelated to whatever you were working on.

## 7. Run the suite

```sh
cd backend && uv run pytest -q
```

The default run already excludes two opt-in marker groups: the object-storage round-trip tests
(which need a separate local object-storage emulator, unrelated to this runbook) and a set of
Alembic up/down round-trips that permanently consume schema resources on a long-lived shared test
database — leave both opted out for routine runs.

**If it fails before any test runs**, naming the database and telling you to create
`backend/.env.test`: your configuration isn't being picked up, or the configured database name
doesn't contain `test` — see step 1.

**If it fails with a permission error connecting to the control-plane database**: step 2 was
skipped, or undone since.

**If the same handful of appdb tests fail, but which ones fail changes between runs**: see the
debris note in step 3.

A clean run from here takes roughly eight minutes.

## Where to go next

- `backend/.env.test.example` — the authoritative shape of every setting this suite reads.
