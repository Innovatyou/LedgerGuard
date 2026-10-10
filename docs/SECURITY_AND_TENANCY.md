# Security and tenant isolation

## Tenant-isolation model

LedgerGuard uses **shared-database, row-level tenancy** by default.

* Every institution-specific model inherits `core.models.InstitutionScopedModel` (an `institution` foreign key)
  or hangs off such a model.
* The active institution is resolved on the server (`core.tenancy`) from the user's **active memberships only**.
  A session value or `X-Institution` API header naming an institution the user is not a member of is ignored (web)
  or rejected with 403 (API). Superusers are platform operators and can access every institution; grant this only to
  the vendor/IT operations team under the institution's privileged-access policy.
* Web views use `InstitutionRequiredMixin` and `scoped_get(...)`, and APIs use `InstitutionScopedViewSet`; every
  queryset is filtered by `institution=` before any object lookup, so foreign records return 404.
* Services re-check that related objects (accounts on journal lines, rules on reconciliations, profiles on imports,
  journals on adjustments) belong to the same institution and raise `TenantViolationError` otherwise.
* Uniqueness (account codes, journal numbers, file checksums, transaction fingerprints) is per institution.
* Automated tests (`tests/test_security.py`) cover URL tampering, session tampering, the API header, and
  cross-tenant foreign keys in API payloads.

### Stronger isolation options

For institutions whose policies forbid co-mingled data:

1. **Dedicated deployment per institution** (recommended for commercial banks): one stack and database per bank.
   No code changes are required; create a single institution.
2. **Database per tenant**: separate PostgreSQL databases with a database router; operationally heavier.
3. **PostgreSQL row-level security** as additional defence in depth: enable RLS on scoped tables with a policy on
   `institution_id = current_setting('ledgerguard.institution_id')::bigint` and set that variable per request in
   middleware. This is not enabled in the current build.

## Access control

* Permissions are action codes (37 codes in `apps/accounts/permissions.py`) bundled into roles; users hold roles through
  memberships per institution. Effective permissions are the union of active memberships. Custom roles can be
  created per institution.
* Branch scope: a membership limited to a branch restricts journals (header and lines), ledger reports, fixed
  assets, loan/deposit listings and the corresponding API endpoints to that branch; users cannot prepare, approve
  or post journals touching other branches. Reconciliations, suspense items and audit records have no branch
  dimension and are institution-wide.
* Configuration control: with `require_config_approval` on (default), changes to accounts, matching rules,
  loan classification bands, deposit product mappings and institution settings become change requests applied only
  after approval by a different user holding `config.approve` (web and API).
* Maker-checker: when `require_independent_approval` is on (default), the preparer of a journal, reconciliation,
  suspense resolution, workpaper or period close cannot approve/post/reopen it (`approvals.services.assert_independent`).
* Least privilege defaults: e.g. accountants cannot approve or post; reconciliation officers cannot approve
  reconciliations; external auditors and management viewers are read-only; exports need `report.export`.

## Authentication

* Passwords: Django PBKDF2 hashing, minimum 12 characters plus common/numeric/similarity validators; new users and
  administrator resets must choose a new password at next sign-in.
* Login throttling: per username+IP and per username lockout (configurable count and duration; cache-backed —
  use Redis in production so limits apply across workers).
* MFA: TOTP (RFC 6238) enrolment and verification; the session is only authenticated after the second factor;
  replayed codes are rejected; institutions can make MFA mandatory.
* Sessions: HTTP-only, SameSite=Lax, secure cookies in production, sliding idle timeout (default 30 minutes),
  expiry at browser close. CSRF protection on all forms.
* API: session or token authentication; generating a new token revokes the old one.

### Institutional identity providers / SSO

The build ships with local accounts and TOTP. To integrate an institution's identity provider:

* **SAML 2.0 / Azure AD / ADFS**: add `djangosaml2` (or `python3-saml`), map the IdP's `NameID`/email to `User`,
  and keep LedgerGuard memberships/roles as the authorisation source (do not map IdP groups to permissions
  without a reviewed mapping table).
* **OpenID Connect** (Azure AD, Keycloak, Okta): add `mozilla-django-oidc`, configure client ID/secret via
  environment variables, and enforce MFA at the IdP.
* Disable local password login for SSO users by setting an unusable password.

## Data protection

* Uploaded files are stored outside the web root (`LEDGERGUARD_MEDIA_ROOT`) under checksum-based names; macro-enabled
  workbooks are rejected and workbooks are parsed values-only, so macros and formulas are never executed.
* Exports neutralise spreadsheet formula injection.
* Content-Security-Policy allows scripts only from the application origin (no inline scripts); frames are denied;
  authentication and API responses are `no-store`. One-time secrets (temporary passwords, integration keys) are
  shown once via server-side session messages and only hashes of integration keys are stored.
* Integration endpoints use hashed bearer keys, IP allow-lists and rate limiting, and act as a named service user so
  every inbound file is attributable and passes normal import validation.
* Evidence uploads are restricted by extension and size; downloads are permission-checked and logged.
* The activity log, approval records and ledger entries are append-only at database level.
* Secrets are read from environment variables; no credentials are stored in the repository.

## Production hardening checklist

* `DJANGO_DEBUG=False`, long random `DJANGO_SECRET_KEY`, explicit `DJANGO_ALLOWED_HOSTS` and
  `DJANGO_CSRF_TRUSTED_ORIGINS`.
* Terminate TLS at a reverse proxy; set `DJANGO_SECURE_SSL_REDIRECT=True` and `DJANGO_HSTS_SECONDS` once HTTPS is verified.
* Use the two database roles from `deploy/postgres/create_roles.sql`: the owner role runs migrations
  (`MIGRATION_DATABASE_URL`), the application role (`DATABASE_URL`) has data access only. `harden_database`
  (run automatically by the container entrypoint) removes UPDATE/DELETE/TRUNCATE on the ledger, approval-record and
  activity-log tables. Verified: the application role cannot update, delete, truncate, or disable/drop triggers.
* Use Redis for `CACHE_URL` (throttling across workers) and Celery.
* Encrypt the database volume and backups; restrict access to the media volume.
* Ship JSON logs to the institution's SIEM; alert on `auth.login_locked`, `period.reopened`, `reconciliation.reopened`
  and `journal.reversed` events.
* `python manage.py check --deploy` must report no errors.
