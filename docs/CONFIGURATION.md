# Configuration reference

All application environment names use the `CHRONOBEAM_` prefix. `ENV` defaults to
`production`; only explicit `dev` or `test` disables production validation. Production settings
are validated at app creation, and validation errors identify a setting without printing its
value. Defaults and validators are in [config.py](../backend/app/config.py#L32-L41) and
[config.py](../backend/app/config.py#L39-L124), with production refusals in
[config.py](../backend/app/config.py#L133-L218).

“Required” below means required by current production startup validation. It does not mean a
deployment value has been supplied. No value in this table is a production value.

| Setting | Type; default | Production requirement/refusal | Secret and owner |
|---|---|---|---|
| `CHRONOBEAM_ENV` | string; `production` | Must be explicitly `production`; unset or another value is refused. **Required.** | No; engineer/deployer. |
| `CHRONOBEAM_DATABASE_URL` | string; localhost development DSN | **Required.** PostgreSQL URL and hostname required; refuses baked-in development DSN, `chronobeam_dev_only`, port `55432`, malformed port, non-PostgreSQL scheme or missing hostname. | Secret-bearing credentials; platform owner. |
| `CHRONOBEAM_TEST_DATABASE_URL` | string; localhost `chronobeam_test` DSN | Not checked by production startup; used by tests and scratch migration checks. | Credentials are secret-bearing if changed; engineer/platform owner. |
| `CHRONOBEAM_CORS_ORIGINS` | string list; `[]` (dev/test defaults to `http://localhost:5173` unless set) | Empty accepted. Configured production origins containing `*`, not using HTTPS or lacking a network location are refused. | No; deployer. |
| `CHRONOBEAM_LOG_LEVEL` | string; `INFO` | Must be one of `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`, `NOTSET`, case-insensitive. | No; engineer/operator. |
| `CHRONOBEAM_FORWARDED_ALLOW_IPS` | string; empty | Empty accepted; each comma-separated nonempty value must parse as an IP network. Sets trusted proxy sources. | No; operator. |
| `CHRONOBEAM_FRONTEND_DIST` | path; repository `frontend/dist` | No production validator; SPA is served if directory exists. Container sets `/app/frontend/dist`. | No; engineer/deployer. |
| `CHRONOBEAM_OIDC_ISSUER` | string; empty | HTTPS URL without wildcard host required. | No; identity owner. |
| `CHRONOBEAM_OIDC_AUDIENCE` | string; empty | Nonempty value required. | No; identity owner. |
| `CHRONOBEAM_OIDC_PUBLIC_KEY` | string; empty | Not required/used for production discovery/JWKS; static-key seam is dev/test only. | Public key, not secret; engineer. |
| `CHRONOBEAM_PUBLIC_BASE_URL` | string; empty | HTTPS URL with network location and no wildcard host required. | No; operator. |
| `CHRONOBEAM_OIDC_DISCOVERY_URL` | string; empty | HTTPS URL with network location and no wildcard host required. | No; identity owner. |
| `CHRONOBEAM_OIDC_CLIENT_ID` | string; empty | Nonempty value required. | No; identity owner. |
| `CHRONOBEAM_OIDC_CLIENT_AUTH_METHOD` | string; `none` | Must be exactly `basic`, `post` or `none`; `private_key_jwt` is unsupported and refused. | No; identity owner. |
| `CHRONOBEAM_OIDC_CLIENT_SECRET` | string; empty | Required only when method is `basic` or `post`; empty then refuses startup. | Yes; secret store. |
| `CHRONOBEAM_SESSION_IDLE_TTL_SECONDS` | integer; `1800` | Must be greater than zero. | No; security owner. |
| `CHRONOBEAM_SESSION_ABSOLUTE_TTL_SECONDS` | integer; `28800` | Must be greater than zero. | No; security owner. |
| `CHRONOBEAM_JWKS_CACHE_TTL_SECONDS` | integer; `300` | Must be greater than zero. | No; identity owner. |
| `CHRONOBEAM_OIDC_CLOCK_SKEW_SECONDS` | integer; `30` | Must be zero or greater. | No; identity owner. |
| `CHRONOBEAM_ENABLE_SHIFT_2026_AUTHORITATIVE_COMPENSATION` | boolean; `false` | No production startup validation. Enabling does not resolve the underlying blocked policy. | No; policy owner with engineer. |
| `CHRONOBEAM_ENABLE_SHIFT_CANCELLATION_3D_AUTO_ENTITLEMENT` | boolean; `false` | No production startup validation; exact boundary and authority remain blocked. | No; policy owner with engineer. |
| `CHRONOBEAM_ENABLE_SHIFT_PLAN_ENTRY` | boolean; `false` | No startup validation; gates employee-entered shift-plan capability. | No; shift-plan owner. |
| `CHRONOBEAM_ENABLE_OCD_PLAN_ENTRY` | boolean; `false` | No startup validation; gates employee-entered OCD-plan capability. | No; OCD owner. |
| `CHRONOBEAM_RECIPIENT_SUBJECTS` | string; empty | No startup refusal. Empty means no recipients are configured and submissions remain blocked. | No; the recipient and purpose list is deployment-owned. |
| `CHRONOBEAM_CUTOVER_OPERATOR_SUBJECTS` | string list; `[]` | No startup refusal. Empty means nobody can seed opening balance. | No; deployer owns allowlist. |
| `CHRONOBEAM_ROLLOUT_BOUNDARY_MONTH` | string; empty | No startup validation. Unset means no additional lower bound. | No; deployment owner. |

For production startup, the settings with no safe production default that must be supplied are
`ENV`, `DATABASE_URL`, `PUBLIC_BASE_URL`, `OIDC_DISCOVERY_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`
and `OIDC_AUDIENCE`. Supply `OIDC_CLIENT_SECRET` when the selected client-authentication method is
`basic` or `post`. Empty CORS and proxy allowlists are accepted; empty recipient and cutover
allowlists disable those respective capabilities. Production values, secret-store location,
approved origins/proxies, recipients, cutover operators and rollout month are supplied by the
deployment owner, not inferred by this document. [Production
validators](../backend/app/config.py#L147-L218).

`TEST_DATABASE_URL`, `OIDC_PUBLIC_KEY` and dev/test authentication/seed seams are not production
configuration paths. Do not place credentials in source, business tables, logs, image layers or
command arguments; the application expects credential delivery to be arranged by the deployment
owner. Secret storage is not established by this repository.