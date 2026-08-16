# Infrastructure

Terraform defines five isolated Cloud Run execution units:

- an API reached only through an external HTTPS load balancer and Cloud Armor
- a private worker owned by the media-integration track
- a completion-oriented media job owned by the media-integration track
- a migration job with database access and no automatic retries
- an A-owned scheduled Outbox relay/event-pump job that publishes committed events and commits in-app intents before ack

The API, private media worker, migration job, and Outbox relay/event pump are always provisioned from the same immutable application image. The media Job remains optional through `job_runtime`. Terraform refuses to destroy a configured runtime implicitly. API, worker, migration, and relay runtimes export OpenTelemetry traces.

The Outbox relay/event pump runs once per minute with one task and no platform retries. It publishes a bounded Outbox batch, consumes a bounded notification batch, and dispatches a bounded background-job batch to Cloud Tasks. Database leases, event receipts, and deterministic task names make overlapping scheduler deliveries safe. Cloud Tasks authenticates with a dedicated OIDC caller to the internal-only worker, attempts each task up to five times, alerts on failed attempts, and raises a warning when tasks remain queued for fifteen minutes. The Pub/Sub topic and source subscription retain messages for 31 days, while Pub/Sub native retry and the retained DLQ cover failures. After the first subscription deployment, run the protected one-time replay workflow described in [`docs/NOTIFICATION_CONSUMER_RUNBOOK.md`](../docs/NOTIFICATION_CONSUMER_RUNBOOK.md); known bootstrap states are a consume no-op until that guarded seek completes.

## Security invariants

- GitHub Actions uses Workload Identity Federation, never a service-account key.
- Published containers are non-root and deployed by immutable Artifact Registry digest.
- The API accepts load-balancer ingress only; the worker remains internal-only.
- Cloud Armor applies managed SQL injection and XSS rules, limits public move-job bootstrap to 10 requests per minute per client IP, limits all `/api/v1/` traffic to 600 requests per minute per client IP, and rate-limits database readiness probes.
- Database and optional Redis URLs come from existing Secret Manager secrets.
- Runtime service accounts are distinct and receive only their required secret, trace, storage, and provider roles. The private worker alone receives `roles/aiplatform.user` for Gemini calls.
- Terraform state uses a pre-created private, versioned GCS bucket.
- Every deployed migration must remain compatible with the previous ready revision. Destructive schema contraction is deployed only after that revision is no longer a rollback target.

One move-job request accepts at most 100 room zones per location so the public bootstrap cannot expand into an unbounded database write.

## Staging prerequisites

Create the role-specific database secrets and at least one Cloud Monitoring notification channel. Configure these GitHub environment variables on `staging`:

| Variable | Purpose |
| --- | --- |
| `GCP_PROJECT_ID` | Dedicated staging project |
| `GCP_REGION` | Cloud Run region, such as `asia-northeast3` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full Workload Identity Provider name |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Deployment identity impersonated through OIDC |
| `GCP_DB_RECOVERY_SERVICE_ACCOUNT` | Separate identity used only by the guarded recovery workflow |
| `TF_STATE_BUCKET` | Existing Terraform state bucket |
| `ARTIFACT_REPOSITORY` | Artifact Registry repository ID |
| `API_DOMAIN` | Public lowercase API DNS name |
| `FRONTEND_ORIGIN` | Exact canonical HTTPS browser origin allowed by API CORS; no port, path, or wildcard |
| `MEDIA_BUCKET_NAME` | Existing private Cloud Storage bucket used for media objects |
| `MIGRATION_DATABASE_URL_SECRET_ID` | Migration-owner database URL secret ID |
| `API_DATABASE_URL_SECRET_ID` | API runtime database URL secret ID |
| `WORKER_DATABASE_URL_SECRET_ID` | Private worker database URL secret ID |
| `OUTBOX_RELAY_DATABASE_URL_SECRET_ID` | Outbox relay database URL secret ID |
| `RECOVERY_DATABASE_URL_SECRET_ID` | Recovery-only read-only database URL secret ID |
| `CLOUD_SQL_SOURCE_INSTANCE` | Fixed same-project, same-region staging PostgreSQL primary instance ID used by deploy and recovery |
| `DB_RECOVERY_CONNECTION_MODE` | Cloud SQL Auth Proxy route: `public` or `private` |
| `MEDIA_RETENTION_DAYS` | Approved whole-number media retention period, from 1 through 3650 days |
| `REDIS_URL_SECRET_ID` | Optional Redis URL secret ID |
| `REDIS_VPC_NETWORK` | Optional existing VPC network for API Direct VPC egress; configure with `REDIS_VPC_SUBNETWORK` |
| `REDIS_VPC_SUBNETWORK` | Optional existing subnet in `GCP_REGION`; configure with `REDIS_VPC_NETWORK` |
| `MONITORING_NOTIFICATION_CHANNELS` | Comma-separated full notification-channel names |

The deployment identity must manage the resources in `infrastructure/terraform`, enable their APIs, access the state prefix, update IAM on `MEDIA_BUCKET_NAME`, and have `iam.serviceAccounts.actAs` plus permission to update IAM on the API, worker, migration, Outbox relay, task-caller, and relay scheduler-caller service accounts. It needs Cloud Tasks queue/IAM, Pub/Sub topic/subscription/IAM, Cloud Scheduler job management, and permission to grant the private worker `roles/aiplatform.user`. The Google-managed Scheduler service agent must retain `roles/cloudscheduler.serviceAgent`; Terraform explicitly preserves `roles/cloudtasks.serviceAgent` for the Cloud Tasks service agent and grants it `iam.serviceAccounts.actAs` on the dedicated task caller. Terraform grants the API service account self-scoped Token Creator for signed URLs; the deployment identity does not need Token Creator itself. The protected replay workflow additionally requires `pubsub.subscriptions.get`, `pubsub.subscriptions.update`, `pubsub.subscriptions.consume`, `run.jobs.get`, `run.jobs.run`, and `run.executions.get`; Terraform does not grant permissions to this external identity. Enable the Cloud Resource Manager API before the first run because the Terraform provider requires it before Terraform can manage project APIs. Terraform enables the Vertex AI API and passes the deployment region to the worker as `SEQRET_ANALYSIS_LOCATION`; the configured Gemini model must support that region. Restrict the Workload Identity Provider to this repository and `main`.

The runtime module mounts `CLOUD_SQL_SOURCE_INSTANCE` at `/cloudsql` for the Gen2 API, private worker, migration gate, and Outbox relay/event pump and grants only those service accounts `roles/cloudsql.client`. This staging path requires a public IPv4 Cloud SQL instance; the authenticated proxy does not need an authorized network. Store one psycopg Unix-socket URL per PostgreSQL role, such as `postgresql+psycopg://USER:PASSWORD@/DATABASE?host=/cloudsql/PROJECT:REGION:INSTANCE`, percent-encoding the URL components, and do not expose the database through an unrestricted authorized network. The deploy and recovery workflows reject a missing, malformed, or duplicated Secret ID. All four deployed runtimes reject a secret whose socket path does not exactly match the mounted instance. The staging workflow caps the API service at two instances with three database connections each so canary revisions fit a small Cloud SQL connection budget. The approved staging budget is based on PostgreSQL `max_connections=25`, three reserved connections, ten ordinary runtime connections, and a conservative eighteen-connection rollout ceiling. Monitoring sums `num_backends` across every database on the instance and warns when it stays above eighteen for two minutes.

The approved A-11 PostgreSQL privilege contract is:

| Runtime | PostgreSQL role | Tables | Sequences | Functions and schema |
| --- | --- | --- | --- | --- |
| migration | `seqret_migration` | owner privileges | owner privileges | owns application objects and keeps `CREATE` on `public`; no `SUPERUSER`, `CREATEDB`, `CREATEROLE`, replication, RLS bypass, or `cloudsqlsuperuser` membership |
| API | `seqret_api` | `SELECT`, `INSERT`, `UPDATE`; no `DELETE`, `TRUNCATE`, `REFERENCES`, or `TRIGGER` | `USAGE`, `SELECT` | `USAGE` on `public` and `EXECUTE` on application trigger functions; no schema `CREATE` |
| private worker | `seqret_worker` | `SELECT`, `INSERT`, `UPDATE`; no `DELETE`, `TRUNCATE`, `REFERENCES`, or `TRIGGER` | `USAGE`, `SELECT` | `USAGE` on `public` and `EXECUTE` on application trigger functions; no schema `CREATE` |
| Outbox relay | `seqret_relay` | `SELECT`, `INSERT`, `UPDATE`; no `DELETE`, `TRUNCATE`, `REFERENCES`, or `TRIGGER` | `USAGE`, `SELECT` | `USAGE` on `public` and `EXECUTE` on application trigger functions; no schema `CREATE` |
| recovery | `seqret_recovery` | `SELECT` only | none | `USAGE` on `public`, no function execution or schema `CREATE`, and `default_transaction_read_only=on` |

Current application deletion is a state transition or external-object operation, so database `DELETE` is intentionally withheld from every runtime. Revoke `CREATE` on `public` and database `TEMPORARY` from `PUBLIC`, then grant them only to the migration owner as needed. Transfer existing application-object ownership from the legacy Cloud SQL administrator `seqret` to `seqret_migration`, and set that role's default privileges so future tables, sequences, and functions preserve the same matrix; otherwise a later migration can silently break or widen a runtime. The existing `audit_event` trigger still protects accidental mutation, but the table owner can alter or disable it. Tamper resistance therefore depends on application traffic using non-owner roles; the migration identity remains an audited maintenance boundary.

Roll out the split without invalidating the stable revision: create and test the roles and five secrets, deploy once while the prior single-secret revision remains the rollback target, then deploy a second time so both the stable and rollback revisions use runtime-specific roles. Only then rotate the legacy `seqret` administrator credential, revoke its direct `CREATEDB` and `CREATEROLE` attributes, and disable the legacy secret version. On failure before that rotation, let the deployment gate return traffic to the prior revision. On failure after rotation, restore the retained legacy credential and Secret version before attempting any pre-split rollback. Never delete a retained credential until migration, API write, worker import, relay lease/publish, recovery read-only, and audit-trigger canaries all pass.

`MEDIA_BUCKET_NAME` must already exist as a private bucket. Terraform gives the API service account bucket-scoped object create/view access and self-scoped URL-signing permission, and gives only the private worker object read/delete access; it does not create or make the bucket public. Configure bucket CORS separately with the approved FE origin, `PUT`, `Content-Type`, and `x-goog-if-generation-match`.

For Memorystore, configure `REDIS_URL_SECRET_ID`, `REDIS_VPC_NETWORK`, and `REDIS_VPC_SUBNETWORK` together. Terraform does not create this network path: the subnet must already be in `GCP_REGION`, use an IPv4 range of `/26` or larger, and belong to the Memorystore instance's authorized network. The Cloud Run service agent (`service-PROJECT_NUMBER@serverless-robot-prod.iam.gserviceaccount.com`) must retain `roles/run.serviceAgent` or receive `roles/compute.networkUser` on the project or subnet. After deployment, confirm the API revision reports the expected network, subnet, and `private-ranges-only` egress, invoke an authenticated API route, and verify a corresponding command or connection in Memorystore metrics. An API success alone is not connection evidence because the database fallback intentionally masks Redis outages. A retryable Redis failure emits `access_rate_limit_cache_fallback` without credential or participant data and opens a rate-limited Monitoring alert while the database limit remains active.

The approved A-10 staging instance is `seqret-stg-cache` in `asia-northeast3`: Basic Tier, 1 GiB, Redis 7.2, AUTH enabled, persistence disabled, and `allkeys-lru`. It uses direct peering to the `default` network; only the API attaches the regional `default` `/20` subnet with private-ranges-only Direct VPC egress. The maintenance window starts Sunday 17:00 UTC (Monday 02:00 KST), where a restart is acceptable because PostgreSQL remains the rate-limit source of truth. The 2026-08-16 Cloud Billing Catalog rate for the Seoul Basic M1 SKU is USD 0.065 per GiB-hour, approximately USD 47.45 for 730 hours, before tax or credits; recheck the [official Memorystore pricing](https://cloud.google.com/memorystore/docs/redis/pricing) before changing tier or capacity. The authenticated URL exists only in `seqret-redis-url`; do not record its host or AUTH value in GitHub, logs, or documentation.

The 2026-08-16 activation evidence is [Deploy staging #31939913364](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31939913364). The API-only revision exposed the configured network, subnet, `private-ranges-only` egress, and `seqret-redis-url`, while the worker exposed none of the Redis settings or VPC attachment. A privacy-safe onboarding canary returned `201` and three authenticated requests returned `200`; Memorystore then recorded positive `eval` and `incr` command counts. A zero-traffic failure revision using an unreachable temporary URL returned `200` for all three authenticated requests through the PostgreSQL source-of-truth limit and emitted three `access_rate_limit_cache_fallback` warnings under the enabled WARNING policy. Traffic was restored to `seqret-stg-api-a10-restore`, the normal canary passed again, and the temporary revision and Secret were removed.

To roll back Redis without weakening access limiting, clear all three staging Redis variables together and redeploy so the API returns to its database-only path. Keep the instance and secret intact until that deployment and an authenticated route are healthy. Delete the exact instance only as a separately approved cost cleanup after the rollback evidence is complete.

The database-recovery identity is separate from the deployment identity. Restrict its `roles/iam.workloadIdentityUser` binding to this repository, `main`, and the protected `staging` environment, and grant `secretmanager.versions.access` on `RECOVERY_DATABASE_URL_SECRET_ID` only. Its project-level custom role needs `cloudsql.instances.clone`, `cloudsql.instances.get`, `cloudsql.instances.list`, `cloudsql.instances.connect`, `cloudsql.instances.update`, and `cloudsql.instances.delete`; it also needs the Cloud SQL operation access used by `gcloud operations get/list/wait/cancel` and `serviceusage.services.use`. Those Cloud SQL permissions are project-wide rather than instance-scoped, so the protected environment and the workflow's generated-target guards are part of the security boundary; use a dedicated staging project. The source instance, SQL Admin API, PITR retention window, quotas, private-service address capacity, organization policies governing authorized networks, and any CMEK service-agent permissions remain externally managed.

`DB_RECOVERY_RUNNER` is an optional repository variable naming the runner label used by public-IP recovery drills; it defaults to `ubuntu-latest`. A `private` connection requires this variable and a Linux amd64 runner with the corresponding VPC path, a current GitHub Actions runner, outbound HTTPS, Git, `curl`, `jq`, and `sha256sum`. The Cloud SQL Auth Proxy authenticates and encrypts a connection but does not create that network path. Never make the source public just for a drill.

## Deployment and rollback

Run `Deploy staging` from `main`. The workflow checks out the current `origin/main` commit and:

1. builds and publishes a uniquely tagged image, then resolves its immutable digest;
2. deploys and executes the migration job before changing the API service;
3. creates the API revision at zero traffic while the current stable revision stays at 100 percent;
4. verifies the managed certificate and database-aware public readiness endpoint, then opens general edge traffic;
5. sends a 10 percent canary share, requires five readiness responses from that revision, then promotes it;
6. restores the previous stable revision automatically if rollout fails or is cancelled while the runner is available.

The first run has no previous revision, so it creates the edge and API before DNS can be configured. General routes remain denied while the throttled health and readiness probes stay reachable. The workflow reports the global address and fails safely until `API_DOMAIN` points there, the managed certificate is active, and `/readyz` reaches the database. Configure DNS and rerun; the bootstrap run is not recorded as a successful deployment.

The first deployment from a revision without the `readiness_contract=v1` label uses its process-only `/healthz` once and does not retain that legacy revision as an operator rollback target. Every revision created by this module is gated by database-aware `/readyz` thereafter.

For an operator rollback, run `Roll back staging` with the one-time rollback target recorded on the current deployment. The workflow requires database readiness, restores the prior traffic allocation on failure, and consumes the target after success. A second chained rollback and arbitrary older revisions are rejected because their schema compatibility is unknown.

## Database recovery drill

Run `Verify staging DB recovery` from the current `main` after a successful staging migration. Supply:

- an RFC3339 UTC restore time inside the source PITR window and after the current migration;
- a move-job UUID that existed before that time;
- the configured source instance ID again as an explicit confirmation.

The workflow serializes with staging deploys, creates only `seqret-stg-recovery-<run-id>-<attempt>`, and never accepts a project, source, or destination override. It verifies the exact clone operation, opens the restored database read-only through a checksum-pinned Cloud SQL Auth Proxy, and checks the single current Alembic head and marker row with the application's database engine. It never changes the staging database Secret or public service. Cleanup verifies the exact operation and instance before disabling deletion protection and backup retention on the clone only, deleting it, and confirming absence.

The run summary is the recovery evidence: source commit, requested and available recovery times, Cloud SQL operation ID, expected and restored migration head, marker result, cleanup result, and run URL. It intentionally excludes database URLs, credentials, addresses, and row contents. Record a successful run at the agreed recovery-drill cadence; workflow code alone does not prove that the external PITR and network path work.

If cancellation or runner loss prevents cleanup, first look up the latest `CLONE` operation for the exact generated `seqret-stg-recovery-<run-id>-<attempt>` target, even when the instance is not visible yet. Verify the operation has the same project and target ID, then wait for or cancel it. On that exact clone only, disable deletion protection, retained backups on delete, and final backup, delete the instance, wait for the delete operation, and confirm the name is absent. Never patch or delete `CLOUD_SQL_SOURCE_INSTANCE`; an unverified recovery instance remains an incident until removed.

## Observability

The API emits JSON logs containing `request_id`, `trace_id`, optional `job_id`, route, status, and duration. Request headers, query strings, bodies, tokens, addresses, and signed URLs are not recorded. W3C `traceparent` is accepted for correlation while local sampling limits export volume. Traces are exported to the Google Telemetry API.

Cloud Monitoring includes request-rate, p95-latency and job-result charts, a 99% rolling 30-day non-5xx request SLO, and alerts for API 5xx responses, p95 latency, Cloud SQL connection pressure, Redis rate-limit database fallbacks, failed media jobs, failed or missing Outbox relay/event-pump executions, saturated Outbox relay batches, notification source backlog age, notification DLQ backlog, and failed external `/edgez` checks. A saturated batch only means the relay claimed its configured publish limit; repeated warnings are the signal to inspect backlog and capacity. The `/edgez` endpoint uses the ordinary policy path, so a closed default policy is reported as unavailable.

The public HTTPS proxy enforces the managed `MODERN` profile with TLS 1.2 as its minimum. Artifact Registry deploys by immutable digest while repository cleanup remains in dry-run: versions older than 90 days are deletion candidates and the newest 50 versions are retained. Review the provider cleanup audit logs before changing dry-run to active deletion.

## Local validation

Terraform `1.15.8` and Google provider `7.44.x` are pinned:

```bash
terraform -chdir=infrastructure/terraform fmt -check -recursive
terraform -chdir=infrastructure/terraform init -backend=false
terraform -chdir=infrastructure/terraform validate
terraform -chdir=infrastructure/terraform test
```

Formatting, static validation, and mocked Terraform tests do not require GCP credentials. A real staging deployment additionally requires the variables and cloud resources above.
