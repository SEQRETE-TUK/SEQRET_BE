# Infrastructure

Terraform defines four isolated Cloud Run execution units:

- an API reached only through an external HTTPS load balancer and Cloud Armor
- a private worker owned by the media-integration track
- a completion-oriented media job owned by the media-integration track
- a migration job with database access and no automatic retries

The API and migration job are always provisioned. The worker and media job are created only when their owning track supplies an immutable image and explicit entrypoint through `worker_runtime` and `job_runtime`; an API release reuses any existing runtime contracts from Terraform state and never substitutes its image for them. Terraform also refuses to destroy a configured integration runtime implicitly. The API and migration job export OpenTelemetry traces, while integration-runtime telemetry stays disabled until its owners instrument it.

## Security invariants

- GitHub Actions uses Workload Identity Federation, never a service-account key.
- Published containers are non-root and deployed by immutable Artifact Registry digest.
- The API accepts load-balancer ingress only; the worker remains internal-only.
- Cloud Armor applies managed SQL injection and XSS rules and rate-limits database readiness probes at the public edge.
- Database and optional Redis URLs come from existing Secret Manager secrets.
- Runtime service accounts are distinct and receive only their required secret and trace roles.
- Terraform state uses a pre-created private, versioned GCS bucket.
- Every deployed migration must remain compatible with the previous ready revision. Destructive schema contraction is deployed only after that revision is no longer a rollback target.

## Staging prerequisites

Create the database secret and at least one Cloud Monitoring notification channel. Configure these GitHub environment variables on `staging`:

| Variable | Purpose |
| --- | --- |
| `GCP_PROJECT_ID` | Dedicated staging project |
| `GCP_REGION` | Cloud Run region, such as `asia-northeast3` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full Workload Identity Provider name |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Deployment identity impersonated through OIDC |
| `TF_STATE_BUCKET` | Existing Terraform state bucket |
| `ARTIFACT_REPOSITORY` | Artifact Registry repository ID |
| `API_DOMAIN` | Public lowercase API DNS name |
| `DATABASE_URL_SECRET_ID` | Existing database URL secret ID |
| `REDIS_URL_SECRET_ID` | Optional Redis URL secret ID |
| `MONITORING_NOTIFICATION_CHANNELS` | Comma-separated full notification-channel names |

The deployment identity must manage the resources in `infrastructure/terraform`, enable their APIs, impersonate the four runtime service accounts, and access the state prefix. Restrict the Workload Identity Provider to this repository and `main`.

`DATABASE_URL_SECRET_ID` must resolve to a database endpoint reachable from Cloud Run. If the staging database uses private IP or the Cloud SQL connector, provision that network path and the deployment-specific client IAM outside this runtime module before deployment.

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

## Observability

The API emits JSON logs containing `request_id`, `trace_id`, optional `job_id`, route, status, and duration. Request headers, query strings, bodies, tokens, addresses, and signed URLs are not recorded. W3C `traceparent` is accepted for correlation while local sampling limits export volume. Traces are exported to the Google Telemetry API.

Cloud Monitoring includes request-rate, p95-latency and job-result charts, a 99% rolling 30-day non-5xx request SLO, and alerts for API 5xx responses, p95 latency, failed media jobs, and failed external `/edgez` checks. That endpoint uses the ordinary policy path, so a closed default policy is reported as unavailable.

## Local validation

Terraform `1.15.8` and Google provider `7.44.x` are pinned:

```bash
terraform -chdir=infrastructure/terraform fmt -check -recursive
terraform -chdir=infrastructure/terraform init -backend=false
terraform -chdir=infrastructure/terraform validate
terraform -chdir=infrastructure/terraform test
```

Formatting, static validation, and mocked Terraform tests do not require GCP credentials. A real staging deployment additionally requires the variables and cloud resources above.
