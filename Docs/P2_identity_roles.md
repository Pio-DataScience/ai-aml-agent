# P2 — Identity and mock roles

P2 establishes the BFF-to-Service A trust boundary and the first policy-based permission layer. The UI requires a valid `pio_session` JWT for every API route except login and registration. Each BFF request to Service A verifies that session, uses its JWT `sub` as the canonical user ID, and sends identity headers with a shared server-side service token. Service A checks the token and checks body/path identity values against those headers.

Policy roles and user mappings come only from `config/aml_catalogs/policy.yaml`; database role labels in the JWT do not grant permissions. In `dev` mode, unknown users default to `manager`; in `jwt` mode they default to `viewer`. Configured users can be keyed by JWT subject ID or email. Viewers can use their own chats and the read-only scenario registry. Scenario creation tools and direct deployment require `scenario.create` on the server. Chat rename, pin, and soft-hide remain available to all roles for their own chats.

## Configuration

Set the same non-empty `SERVICE_AUTH_TOKEN` in Service A's root `.env` and the UI's `.env.local`. Keep it server-side; do not prefix it with `NEXT_PUBLIC_`. Service A refuses to start when the value is missing.

`CORS_ALLOWED_ORIGINS` is a comma-separated list in Service A's `.env`; its default is `http://localhost:3000`, matching the UI's default Next.js development origin. The UI example includes the same variable for reference. The active `auth_mode` and user-role mapping are in `config/aml_catalogs/policy.yaml`.

## Run and verify

From the Service A repository root:

```powershell
.\run_dev.ps1
```

From the UI repository root:

```powershell
npm run dev
```

Offline Service A identity/policy checks and the startup-token guard:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m pytest -q services/aml_builder/tests/test_p2_identity_policy.py
.\.venv\Scripts\python.exe -m pytest -q services/aml_builder/tests/test_p0_settings_and_sse.py -k "service_startup_fails_clearly_without_service_token"
```

The package verification commands are:

```powershell
$env:DEBUG = "false"
.\.venv\Scripts\python.exe -m compileall -q services app.py run_alert_engine.py
.\.venv\Scripts\python.exe -m pytest -q
npm run lint
npm run build
```

## Manual role checks

Add test users to the YAML `users` map using their exact JWT subject IDs or email addresses; email matching is case-insensitive. The development defaults include IDs `999` (manager), `1001` (creator), and `1002` (viewer). Real test emails are intentionally not committed.

1. Log in as a viewer. Confirm the sidebar reports `viewer`, chat history shows only that user's chats, and rename/pin/soft-hide continue to work. Ask a registry question and confirm it can be answered from the registry tool. Ask to create a scenario and confirm the tool returns a permission denial. The deploy button is disabled with a role tooltip.
2. Log in as a creator. Confirm registry queries and scenario creation/deployment are allowed. Confirm scenario revise, rollback, and delete actions are not granted by the configured action list.
3. Log in as a manager. Confirm the permission response includes all five configured actions. P2 does not add revise/rollback scenario controls; this check verifies the policy response.
4. Log out and call a protected BFF API route; it should return HTTP 401. Login and registration should remain reachable without a session.
5. Send a missing or incorrect `X-Service-Token` to a protected Service A route, or send a body/path user or project ID that differs from the identity headers; Service A should return HTTP 403. Do not put the shared token in browser code or browser storage.

No Service B changes are included in P2.
