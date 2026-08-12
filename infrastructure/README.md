# Infrastructure foundation

`FND-A04` provides the validation and deployment skeleton for three isolated runtime units:

- public-edge API service whose Cloud Run ingress only accepts internal or load-balancer traffic
- private worker service for authenticated Cloud Tasks and Pub/Sub delivery
- completion-oriented Cloud Run Job

Each runtime has its own service account. Terraform does not grant application permissions to those accounts; later feature owners must add only the roles required by their adapters and handlers.

## Security invariants

- GitHub Actions authenticates through Workload Identity Federation and never consumes a long-lived service-account key.
- The OIDC provider must restrict `attribute.repository` to `SEQRETE-TUK/SEQRET_BE` and should also restrict the staging environment or main branch.
- Container images must be immutable Artifact Registry references ending in `@sha256:<digest>`.
- The API is not granted unauthenticated invocation and only accepts internal or load-balancer ingress.
- Worker ingress is internal-only. No public IAM binding is created.
- Runtime service accounts are separate and receive no broad project roles in this foundation.
- Cloud Run deletion protection defaults to enabled.
- Terraform state uses a pre-created, versioned, private GCS bucket with uniform bucket-level access.

## Required GitHub staging variables

Configure these as GitHub environment variables on the `staging` environment:

| Variable | Purpose |
| --- | --- |
| `GCP_PROJECT_ID` | Dedicated staging GCP project ID |
| `GCP_REGION` | Cloud Run region, for example `asia-northeast3` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full Workload Identity Provider resource name |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Short-lived deployment identity impersonated by GitHub Actions |
| `TF_STATE_BUCKET` | Existing private GCS bucket used for Terraform state |

The deployment identity needs only the permissions required to manage the resources in `infrastructure/terraform`, impersonate the three runtime service accounts, enable the declared APIs and read/write the configured state prefix. Define those permissions outside this repository according to the organization's bootstrap process.

## Local validation

Terraform `1.15.8` and Google provider `7.44.x` are pinned. From the repository root:

```bash
terraform -chdir=infrastructure/terraform fmt -check -recursive
terraform -chdir=infrastructure/terraform init -backend=false
terraform -chdir=infrastructure/terraform validate
terraform -chdir=infrastructure/terraform test
```

No credential is required for formatting, initialization with the backend disabled or static validation.

## Staging workflow

Run `Deploy staging infrastructure` manually with an immutable image reference. The default `apply=false` creates a plan only. After reviewing that result, re-run with `apply=true`. The apply run creates a fresh plan from the current remote state and applies exactly the plan file produced in that same run under the GitHub `staging` environment. It does not reuse the binary plan from the earlier plan-only run.

Configure required reviewers on the `staging` environment before enabling apply runs. Keep `apply=false` until the Workload Identity, state bucket and deployment permissions have been verified.

The GCS state bucket and Workload Identity Federation configuration are bootstrap prerequisites. The workflow fails before authentication when a required variable or immutable image digest is missing.

## Deferred to A-11

This foundation intentionally leaves these production concerns for `A-11`:

- Artifact Registry image build and publication
- database migration-before-traffic gate
- external Load Balancer and Cloud Armor configuration
- OpenTelemetry export, dashboards, SLOs and alerts
- progressive traffic rollout, rollback automation and recovery drills

Worker and Job command values remain explicit Terraform inputs until the B-owned handlers are merged on `main`.
