# B8 Oracle connection roles

## What changed

Service A now treats `ORACLE_*` as its existing registry/application connection
and uses a separate DWH reader connection for generated SQL, schema/lookup
reads, and shadow execution. `DWH_ORACLE_*` is preferred; explicitly configured
legacy `SHADOW_ORACLE_*` values are accepted as an alias. Neither path falls
back to `ORACLE_*`. The registry schema and application table prefix are
configurable. Normal deployment no longer provisions registry tables; the
explicit provisioner is disabled unless `ORACLE_REGISTRY_SCHEMA_PROVISIONING_ENABLED`
is enabled for a separately authorized operation.

## Configuration

Keep the existing `ORACLE_DSN`, `ORACLE_USER`, and `ORACLE_PASSWORD` unchanged
to preserve the currently provisioned registry location. Configure the DWH
reader independently before invoking Oracle-backed DWH features:

```dotenv
DWH_ORACLE_DSN=<dwh-endpoint>
DWH_ORACLE_USER=<read-only-user>
DWH_ORACLE_PASSWORD=<secret>
DWH_ORACLE_SCHEMA=<optional-default-schema>
DWH_ORACLE_POOL_MIN=1
DWH_ORACLE_POOL_MAX=4
ORACLE_REGISTRY_SCHEMA=<optional-registry-schema>
ORACLE_REGISTRY_NAMESPACE=PIO_AML_AGENT
ORACLE_REGISTRY_SCHEMA_PROVISIONING_ENABLED=false
```

An explicitly supplied `SHADOW_ORACLE_DSN/USER/PASSWORD` remains a legacy
alias for the DWH role. There is no fallback from either role to the other.
Schema values and namespace are validated as identifiers. Phase 2A configured
Service A's DWH endpoint from the existing Service B environment; the registry
endpoint remains unchanged. The configured DWH account must be separately
reviewed for database grants; using SELECT-only statements does not make
elevated credentials read-only.

## Verification

Run the offline routing and registry tests from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-temp services\aml_builder\tests\test_oracle_connection_roles.py services\aml_builder\tests\test_p4_oracle_repo.py services\aml_builder\tests\test_p10_vector_cache.py
```

The tests use fake pools and assert role-specific acquisition, missing-reader
failure without registry fallback, configurable namespace translation, and
deployment's lack of runtime schema provisioning. They make no Oracle or
Service B calls. The shadow helper also accepts a stricter per-statement timeout
for controlled smoke checks. Schema provisioning requires the explicit flag
and is not a normal application startup/deployment operation.

## Compatibility and remaining risks

The active registry target and credentials remain the existing `ORACLE_*`
values. DWH-backed lookup, retrieval, and shadow operations now require explicit
reader credentials; if absent, those calls fail clearly. OPENBI2 registry
provisioning/migration is not included. Alert-result DML still uses the
application pool and needs a separately designed writer before that pool can
be restricted to registry tables only.
