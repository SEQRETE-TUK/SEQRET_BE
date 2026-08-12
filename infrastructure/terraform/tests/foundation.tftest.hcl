mock_provider "google" {}

variables {
  project_id      = "seqret-staging"
  region          = "asia-northeast3"
  environment     = "staging"
  api_domain      = "api.staging.example.com"
  container_image = "asia-northeast3-docker.pkg.dev/seqret-staging/backend/app@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

run "staging_runtime_isolation" {
  command = plan

  assert {
    condition     = google_cloud_run_v2_service.api.name == "seqret-stg-api"
    error_message = "The staging API name must remain deterministic."
  }

  assert {
    condition     = google_cloud_run_v2_service.api.ingress == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
    error_message = "The API must only accept internal or load-balancer ingress."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.default_uri_disabled,
      google_cloud_run_v2_service.api.launch_stage == "BETA",
    ])
    error_message = "The API default URL must not bypass the load balancer and Cloud Armor."
  }

  assert {
    condition     = length(google_cloud_run_v2_service.worker) == 0 && length(google_cloud_run_v2_job.job) == 0
    error_message = "B-owned runtimes must not be provisioned without their explicit image and entrypoint contracts."
  }

  assert {
    condition = length(toset([
      google_service_account.api.account_id,
      google_service_account.worker.account_id,
      google_service_account.job.account_id,
      google_service_account.migration.account_id,
    ])) == 4
    error_message = "API, worker, job and migration must use distinct service accounts."
  }

  assert {
    condition = (
      toset(keys(google_project_service.required)) ==
      toset([
        "artifactregistry.googleapis.com",
        "compute.googleapis.com",
        "iam.googleapis.com",
        "run.googleapis.com",
        "secretmanager.googleapis.com",
      ])
    )
    error_message = "The runtime foundation must enable its required Google Cloud APIs."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.deletion_protection,
      google_cloud_run_v2_job.migration.deletion_protection,
    ])
    error_message = "Deletion protection must default to enabled."
  }

  assert {
    condition = alltrue([
      google_artifact_registry_repository.backend.repository_id == "backend",
      google_artifact_registry_repository.backend.docker_config[0].immutable_tags,
    ])
    error_message = "Artifact Registry must reject mutable tag replacement."
  }

  assert {
    condition = alltrue([
      google_compute_backend_service.api.load_balancing_scheme == "EXTERNAL_MANAGED",
      length([
        for rule in google_compute_security_policy.api.rule : rule
        if rule.action == "deny(403)"
      ]) == 2,
      length([
        for rule in google_compute_security_policy.api.rule : rule
        if rule.action == "throttle"
      ]) == 1,
      one([
        for rule in google_compute_security_policy.api.rule : rule.action
        if rule.priority == 2147483647
      ]) == "deny(502)",
      google_compute_global_forwarding_rule.api_https.port_range == "443",
      google_monitoring_uptime_check_config.api.http_check[0].path == "/edgez",
    ])
    error_message = "The public API edge must enforce Cloud Armor, HTTPS, and external health checks."
  }

  assert {
    condition = (
      toset(keys(google_project_service.observability)) ==
      toset([
        "cloudtrace.googleapis.com",
        "logging.googleapis.com",
        "monitoring.googleapis.com",
        "telemetry.googleapis.com",
      ])
    )
    error_message = "The observability APIs must be enabled explicitly."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.template[0].containers[0].startup_probe[0].http_get[0].path == "/healthz",
      google_cloud_run_v2_service.api.template[0].containers[0].liveness_probe[0].http_get[0].path == "/healthz",
    ])
    error_message = "The API must expose startup and liveness health probes."
  }

  assert {
    condition     = google_cloud_run_v2_job.migration.template[0].template[0].max_retries == 0
    error_message = "The deployment migration gate must not retry schema changes implicitly."
  }

  assert {
    condition = join(
      " ",
      google_cloud_run_v2_job.migration.template[0].template[0].containers[0].command,
    ) == "python -m app.entrypoints.migrate"
    error_message = "The deployment migration gate must use the explicit migration entrypoint."
  }

  assert {
    condition = alltrue([
      google_project_iam_member.api_trace_writer.role == "roles/telemetry.tracesWriter",
      google_project_iam_member.migration_trace_writer.role == "roles/telemetry.tracesWriter",
      google_project_iam_member.api_telemetry_consumer.role == "roles/serviceusage.serviceUsageConsumer",
      google_project_iam_member.migration_telemetry_consumer.role == "roles/serviceusage.serviceUsageConsumer",
    ])
    error_message = "Only runtimes that emit traces receive the telemetry writer role."
  }

  assert {
    condition = alltrue([
      google_monitoring_alert_policy.api_server_errors.severity == "ERROR",
      google_monitoring_alert_policy.api_latency.severity == "WARNING",
      google_monitoring_alert_policy.job_failures.severity == "ERROR",
      google_monitoring_slo.api_availability.goal == 0.99,
    ])
    error_message = "Core API and job alert policies must remain enabled at explicit severities."
  }
}

run "production_names_are_isolated" {
  command = plan

  variables {
    environment = "production"
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.name == "seqret-prd-api",
      google_service_account.worker.account_id == "seqret-prd-worker",
      google_service_account.job.account_id == "seqret-prd-job",
    ])
    error_message = "Production resource names must use the prd environment suffix."
  }
}

run "required_labels_cannot_be_overridden" {
  command = plan

  variables {
    labels = {
      application = "incorrect"
      environment = "incorrect"
      managed_by  = "incorrect"
      owner       = "platform"
    }
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.labels["application"] == "seqret",
      google_cloud_run_v2_service.api.labels["environment"] == "staging",
      google_cloud_run_v2_service.api.labels["managed_by"] == "terraform",
      google_cloud_run_v2_service.api.labels["owner"] == "platform",
    ])
    error_message = "Required labels must remain immutable while additional labels are preserved."
  }
}

run "mutable_image_is_rejected" {
  command = plan

  variables {
    container_image = "asia-northeast3-docker.pkg.dev/seqret-staging/backend/app:latest"
  }

  expect_failures = [var.container_image]
}

run "foreign_registry_image_is_rejected" {
  command = plan

  variables {
    container_image = "ghcr.io/seqret/backend@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }

  expect_failures = [var.container_image]
}

run "foreign_project_image_is_rejected" {
  command = plan

  variables {
    container_image = "asia-northeast3-docker.pkg.dev/other-project/backend/app@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }

  expect_failures = [var.container_image]
}

run "foreign_region_image_is_rejected" {
  command = plan

  variables {
    container_image = "us-central1-docker.pkg.dev/seqret-staging/backend/app@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }

  expect_failures = [var.container_image]
}

run "invalid_label_is_rejected" {
  command = plan

  variables {
    labels = {
      Invalid = "UPPERCASE"
    }
  }

  expect_failures = [var.labels]
}

run "oversized_service_name_is_rejected" {
  command = plan

  variables {
    service_name = "s123456789012345678901234567890123456789012"
  }

  expect_failures = [var.service_name]
}

run "oversized_resource_prefix_is_rejected" {
  command = plan

  variables {
    name_prefix = "seqret-resource-name-too-long"
  }

  expect_failures = [var.name_prefix]
}

run "public_edge_opens_only_after_the_gate" {
  command = plan

  variables {
    public_traffic_enabled = true
  }

  assert {
    condition = one([
      for rule in google_compute_security_policy.api.rule : rule.action
      if rule.priority == 2147483647
    ]) == "allow"
    error_message = "General public traffic must open only when the deployment gate enables it explicitly."
  }
}

run "integration_runtimes_require_explicit_contracts" {
  command = plan

  variables {
    worker_runtime = {
      container_image = "asia-northeast3-docker.pkg.dev/seqret-staging/backend/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111"
      command         = ["python", "-m", "app.entrypoints.worker"]
    }
    job_runtime = {
      container_image = "asia-northeast3-docker.pkg.dev/seqret-staging/backend/job@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      command         = ["python", "-m", "app.entrypoints.media_jobs"]
    }
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.worker[0].ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY",
      google_cloud_run_v2_service.worker[0].template[0].containers[0].image == var.worker_runtime.container_image,
      google_cloud_run_v2_job.job[0].template[0].template[0].containers[0].image == var.job_runtime.container_image,
      google_cloud_run_v2_service.worker[0].deletion_protection,
      google_cloud_run_v2_job.job[0].deletion_protection,
    ])
    error_message = "B-owned runtimes must use their explicit immutable images and safe platform defaults."
  }
}

run "invalid_observability_inputs_are_rejected" {
  command = plan

  variables {
    otel_trace_sample_ratio = 1.1
  }

  expect_failures = [var.otel_trace_sample_ratio]
}

run "invalid_api_domain_is_rejected" {
  command = plan

  variables {
    api_domain = "https://api.example.com/path"
  }

  expect_failures = [var.api_domain]
}

run "stable_revision_holds_latest_at_zero" {
  command = plan

  variables {
    stable_api_revision = "seqret-stg-api-abcde"
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.traffic[0].revision == "seqret-stg-api-abcde",
      google_cloud_run_v2_service.api.traffic[0].percent == 100,
      google_cloud_run_v2_service.api.traffic[1].percent == 0,
      google_cloud_run_v2_service.api.template[0].labels.readiness_contract == "v1",
    ])
    error_message = "A new revision must receive zero traffic while the existing stable revision remains at 100 percent."
  }
}

run "foreign_stable_revision_is_rejected" {
  command = plan

  variables {
    stable_api_revision = "another-service-00001-abc"
  }

  expect_failures = [check.stable_api_revision_belongs_to_service]
}

run "invalid_notification_channel_is_rejected" {
  command = plan

  variables {
    monitoring_notification_channel_ids = ["invalid/channel"]
  }

  expect_failures = [var.monitoring_notification_channel_ids]
}
