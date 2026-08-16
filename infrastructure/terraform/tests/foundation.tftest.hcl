mock_provider "google" {}

variables {
  project_id            = "seqret-staging"
  region                = "asia-northeast3"
  environment           = "staging"
  api_domain            = "api.staging.example.com"
  frontend_origin       = "https://staging.example.com"
  media_bucket_name     = "seqret-stg-media"
  container_image       = "asia-northeast3-docker.pkg.dev/seqret-staging/backend/app@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  cloud_sql_instance_id = "seqret-stg-db"
  api_max_instances     = 2
  media_retention_days  = 30
}

run "staging_runtime_isolation" {
  command = apply

  assert {
    condition = one([
      for env in google_cloud_run_v2_service.api.template[0].containers[0].env : env.value
      if env.name == "SEQRET_FRONTEND_ORIGIN"
    ]) == "https://staging.example.com"
    error_message = "The API must receive the exact configured browser origin."
  }

  assert {
    condition = alltrue([
      one([
        for env in google_cloud_run_v2_service.api.template[0].containers[0].env : env.value
        if env.name == "SEQRET_MEDIA_BUCKET_NAME"
      ]) == "seqret-stg-media",
      one([
        for env in google_cloud_run_v2_service.api.template[0].containers[0].env : env.value
        if env.name == "SEQRET_STORAGE_SIGNING_SERVICE_ACCOUNT_EMAIL"
      ]) == google_service_account.api.email,
      google_storage_bucket_iam_member.api_media_object_creator.bucket == "seqret-stg-media",
      google_storage_bucket_iam_member.api_media_object_creator.role == "roles/storage.objectCreator",
      google_storage_bucket_iam_member.api_media_object_creator.member == "serviceAccount:${google_service_account.api.email}",
      google_storage_bucket_iam_member.api_media_object_viewer.bucket == "seqret-stg-media",
      google_storage_bucket_iam_member.api_media_object_viewer.role == "roles/storage.objectViewer",
      google_storage_bucket_iam_member.api_media_object_viewer.member == "serviceAccount:${google_service_account.api.email}",
      google_service_account_iam_member.api_self_token_creator.service_account_id == "projects/seqret-staging/serviceAccounts/seqret-stg-api@seqret-staging.iam.gserviceaccount.com",
      google_service_account_iam_member.api_self_token_creator.role == "roles/iam.serviceAccountTokenCreator",
      google_service_account_iam_member.api_self_token_creator.member == "serviceAccount:${google_service_account.api.email}",
    ])
    error_message = "The API must receive the existing media bucket and only its upload, read, and signing permissions."
  }

  assert {
    condition     = google_cloud_run_v2_service.api.name == "seqret-stg-api"
    error_message = "The staging API name must remain deterministic."
  }

  assert {
    condition = one([
      for env in google_cloud_run_v2_service.api.template[0].containers[0].env : env.value
      if env.name == "SEQRET_MEDIA_RETENTION_DAYS"
    ]) == "30"
    error_message = "The API must receive the approved media-retention policy."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.template[0].execution_environment == "EXECUTION_ENVIRONMENT_GEN2",
      google_cloud_run_v2_service.api.template[0].max_instance_request_concurrency == 3,
      google_cloud_run_v2_service.api.scaling[0].max_instance_count == 2,
      one([
        for env in google_cloud_run_v2_service.api.template[0].containers[0].env : env.value
        if env.name == "SEQRET_DATABASE_POOL_SIZE"
      ]) == "2",
      one([
        for env in google_cloud_run_v2_service.api.template[0].containers[0].env : env.value
        if env.name == "SEQRET_DATABASE_MAX_OVERFLOW"
      ]) == "1",
      one([
        for env in google_cloud_run_v2_service.api.template[0].containers[0].env : env.value
        if env.name == "SEQRET_DATABASE_SOCKET_PATH"
      ]) == "/cloudsql/seqret-staging:asia-northeast3:seqret-stg-db",
      one([
        for env in google_cloud_run_v2_job.migration.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_DATABASE_SOCKET_PATH"
      ]) == "/cloudsql/seqret-staging:asia-northeast3:seqret-stg-db",
    ])
    error_message = "The staging API must stay within the Cloud SQL connection budget."
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
    condition = alltrue([
      length(google_cloud_run_v2_job.job) == 0,
      google_cloud_run_v2_service.worker.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY",
      google_cloud_run_v2_service.worker.template[0].containers[0].image == var.container_image,
      join(" ", google_cloud_run_v2_service.worker.template[0].containers[0].command) == "python -m uvicorn app.entrypoints.worker:app",
      one([
        for env in google_cloud_run_v2_service.worker.template[0].containers[0].env : env.value
        if env.name == "SEQRET_ANALYSIS_LOCATION"
      ]) == "asia-northeast3",
      google_cloud_run_v2_service.worker.deletion_protection,
    ])
    error_message = "The private media worker must be provisioned with the current immutable application image."
  }

  assert {
    condition     = length(google_cloud_run_v2_service.api.template[0].vpc_access) == 0
    error_message = "The API must not use VPC egress when Redis is not configured."
  }

  assert {
    condition = length(toset([
      google_service_account.api.account_id,
      google_service_account.worker.account_id,
      google_service_account.job.account_id,
      google_service_account.migration.account_id,
      google_service_account.outbox_relay.account_id,
      google_service_account.outbox_scheduler.account_id,
      google_service_account.task_invoker.account_id,
    ])) == 7
    error_message = "Every runtime and scheduler caller must use a distinct service account."
  }

  assert {
    condition = (
      toset(keys(google_project_service.required)) ==
      toset([
        "aiplatform.googleapis.com",
        "artifactregistry.googleapis.com",
        "cloudscheduler.googleapis.com",
        "cloudtasks.googleapis.com",
        "compute.googleapis.com",
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "pubsub.googleapis.com",
        "run.googleapis.com",
        "secretmanager.googleapis.com",
        "sqladmin.googleapis.com",
        "storage.googleapis.com",
      ])
    )
    error_message = "The runtime foundation must enable its required Google Cloud APIs."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.deletion_protection,
      google_cloud_run_v2_service.worker.deletion_protection,
      google_cloud_run_v2_job.migration.deletion_protection,
      google_cloud_run_v2_job.outbox_relay.deletion_protection,
    ])
    error_message = "Deletion protection must default to enabled."
  }

  assert {
    condition = alltrue([
      google_pubsub_topic.events.message_retention_duration == "2678400s",
      google_pubsub_topic.events.deletion_policy == "PREVENT",
      google_cloud_run_v2_job.outbox_relay.template[0].task_count == 1,
      google_cloud_run_v2_job.outbox_relay.template[0].parallelism == 1,
      google_cloud_run_v2_job.outbox_relay.template[0].template[0].max_retries == 0,
      google_cloud_run_v2_job.outbox_relay.template[0].template[0].timeout == "240s",
      join(" ", google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].command) == "python -m app.entrypoints.outbox_relay",
      google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].volume_mounts[0].mount_path == "/cloudsql",
      one(google_cloud_run_v2_job.outbox_relay.template[0].template[0].volumes[0].cloud_sql_instance[0].instances) == "seqret-staging:asia-northeast3:seqret-stg-db",
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_PUBSUB_TOPIC_ID"
      ]) == "seqret-stg-events",
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_TASK_QUEUE_NAME"
      ]) == google_cloud_tasks_queue.media.name,
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_TASK_WORKER_URL"
      ]) == google_cloud_run_v2_service.worker.uri,
      google_cloud_tasks_queue.media.rate_limits[0].max_concurrent_dispatches == var.worker_max_instances,
      google_cloud_tasks_queue.media.retry_config[0].max_attempts == 5,
      google_cloud_tasks_queue.media.retry_config[0].max_retry_duration == "0s",
      google_cloud_tasks_queue.media.retry_config[0].min_backoff == "10s",
      google_cloud_tasks_queue.media.retry_config[0].max_backoff == "600s",
      google_cloud_tasks_queue.media.retry_config[0].max_doublings == 5,
      google_cloud_tasks_queue_iam_member.outbox_relay_enqueuer.role == "roles/cloudtasks.enqueuer",
      google_cloud_tasks_queue_iam_member.outbox_relay_enqueuer.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_service_account_iam_member.outbox_relay_task_invoker_user.role == "roles/iam.serviceAccountUser",
      google_service_account_iam_member.outbox_relay_task_invoker_user.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_service_account_iam_member.cloud_tasks_task_invoker_user.role == "roles/iam.serviceAccountUser",
      google_service_account_iam_member.cloud_tasks_task_invoker_user.member == local.cloud_tasks_service_agent,
      google_project_iam_member.cloud_tasks_service_agent.role == "roles/cloudtasks.serviceAgent",
      google_project_iam_member.cloud_tasks_service_agent.member == local.cloud_tasks_service_agent,
      google_cloud_run_v2_service_iam_member.task_invoker_worker.role == "roles/run.invoker",
      google_cloud_run_v2_service_iam_member.task_invoker_worker.member == "serviceAccount:${google_service_account.task_invoker.email}",
      google_pubsub_topic_iam_member.outbox_relay_publisher.role == "roles/pubsub.publisher",
      google_pubsub_topic_iam_member.outbox_relay_publisher.topic == google_pubsub_topic.events.name,
      google_pubsub_topic_iam_member.outbox_relay_publisher.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_project_iam_member.outbox_relay_cloud_sql_client.role == "roles/cloudsql.client",
      google_project_iam_member.outbox_relay_cloud_sql_client.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_project_iam_member.outbox_relay_trace_writer.role == "roles/telemetry.tracesWriter",
      google_project_iam_member.outbox_relay_trace_writer.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_project_iam_member.outbox_relay_telemetry_consumer.role == "roles/serviceusage.serviceUsageConsumer",
      google_project_iam_member.outbox_relay_telemetry_consumer.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_secret_manager_secret_iam_member.outbox_relay_database.secret_id == "seqret-database-url",
      google_secret_manager_secret_iam_member.outbox_relay_database.role == "roles/secretmanager.secretAccessor",
      google_secret_manager_secret_iam_member.outbox_relay_database.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_cloud_run_v2_job.outbox_relay.template[0].template[0].service_account == google_service_account.outbox_relay.email,
      google_cloud_run_v2_job_iam_member.outbox_scheduler_invoker.role == "roles/run.invoker",
      google_cloud_run_v2_job_iam_member.outbox_scheduler_invoker.member == "serviceAccount:${google_service_account.outbox_scheduler.email}",
      google_cloud_scheduler_job.outbox_relay.schedule == "* * * * *",
      google_cloud_scheduler_job.outbox_relay.deletion_policy == "PREVENT",
      google_cloud_scheduler_job.outbox_relay.retry_config[0].retry_count == 0,
      google_cloud_scheduler_job.outbox_relay.http_target[0].http_method == "POST",
      google_cloud_scheduler_job.outbox_relay.http_target[0].uri == "https://run.googleapis.com/v2/projects/seqret-staging/locations/asia-northeast3/jobs/seqret-stg-relay:run",
      length(google_cloud_scheduler_job.outbox_relay.http_target[0].oauth_token) == 1,
      google_cloud_scheduler_job.outbox_relay.http_target[0].oauth_token[0].service_account_email == google_service_account.outbox_scheduler.email,
    ])
    error_message = "The scheduled Outbox relay must keep its bounded runtime, retained topic, and least-privilege IAM graph."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.template[0].containers[0].volume_mounts[0].mount_path == "/cloudsql",
      one(google_cloud_run_v2_service.api.template[0].volumes[0].cloud_sql_instance[0].instances) == "seqret-staging:asia-northeast3:seqret-stg-db",
      google_cloud_run_v2_job.migration.template[0].template[0].containers[0].volume_mounts[0].mount_path == "/cloudsql",
      one(google_cloud_run_v2_job.migration.template[0].template[0].volumes[0].cloud_sql_instance[0].instances) == "seqret-staging:asia-northeast3:seqret-stg-db",
      google_project_iam_member.api_cloud_sql_client.role == "roles/cloudsql.client",
      google_project_iam_member.worker_cloud_sql_client.role == "roles/cloudsql.client",
      google_project_iam_member.worker_cloud_sql_client.member == "serviceAccount:${google_service_account.worker.email}",
      google_project_iam_member.worker_vertex_ai_user.role == "roles/aiplatform.user",
      google_project_iam_member.worker_vertex_ai_user.member == "serviceAccount:${google_service_account.worker.email}",
      google_project_iam_member.migration_cloud_sql_client.role == "roles/cloudsql.client",
      google_secret_manager_secret_iam_member.worker_database.role == "roles/secretmanager.secretAccessor",
      google_secret_manager_secret_iam_member.worker_database.member == "serviceAccount:${google_service_account.worker.email}",
      google_storage_bucket_iam_member.worker_media_objects.role == "roles/storage.objectUser",
      google_storage_bucket_iam_member.worker_media_objects.bucket == var.media_bucket_name,
      google_storage_bucket_iam_member.worker_media_objects.member == "serviceAccount:${google_service_account.worker.email}",
    ])
    error_message = "The runtimes must keep their authenticated Cloud SQL, media, and Vertex AI permissions."
  }

  assert {
    condition = alltrue([
      google_artifact_registry_repository.backend.repository_id == "backend",
      !google_artifact_registry_repository.backend.docker_config[0].immutable_tags,
      google_artifact_registry_repository.backend.cleanup_policy_dry_run,
      one([
        for policy in google_artifact_registry_repository.backend.cleanup_policies : policy.condition[0].older_than
        if policy.id == "delete-older-than-90-days"
      ]) == "7776000s",
      one([
        for policy in google_artifact_registry_repository.backend.cleanup_policies : policy.most_recent_versions[0].keep_count
        if policy.id == "keep-most-recent-50"
      ]) == 50,
    ])
    error_message = "Artifact Registry cleanup must stay in dry-run with the approved retention boundaries."
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
      ]) == 3,
      one([
        for rule in google_compute_security_policy.api.rule : rule
        if rule.priority == 800
      ]).match[0].expr[0].expression == "request.method == 'POST' && request.path == '/api/v1/move-jobs'",
      one([
        for rule in google_compute_security_policy.api.rule : rule
        if rule.priority == 800
      ]).rate_limit_options[0].rate_limit_threshold[0].count == 10,
      one([
        for rule in google_compute_security_policy.api.rule : rule
        if rule.priority == 850
      ]).match[0].expr[0].expression == "request.path.startsWith('/api/v1/')",
      one([
        for rule in google_compute_security_policy.api.rule : rule
        if rule.priority == 850
      ]).rate_limit_options[0].rate_limit_threshold[0].count == 600,
      one([
        for rule in google_compute_security_policy.api.rule : rule.action
        if rule.priority == 2147483647
      ]) == "deny(502)",
      google_compute_global_forwarding_rule.api_https.port_range == "443",
      google_compute_managed_ssl_certificate.api.name == "seqret-stg-api-cert-6d0cd4bd",
      google_compute_ssl_policy.api.profile == "MODERN",
      google_compute_ssl_policy.api.min_tls_version == "TLS_1_2",
      google_compute_target_https_proxy.api.ssl_policy == google_compute_ssl_policy.api.id,
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
      google_project_iam_member.worker_trace_writer.role == "roles/telemetry.tracesWriter",
      google_project_iam_member.migration_trace_writer.role == "roles/telemetry.tracesWriter",
      google_project_iam_member.api_telemetry_consumer.role == "roles/serviceusage.serviceUsageConsumer",
      google_project_iam_member.worker_telemetry_consumer.role == "roles/serviceusage.serviceUsageConsumer",
      google_project_iam_member.migration_telemetry_consumer.role == "roles/serviceusage.serviceUsageConsumer",
    ])
    error_message = "Only runtimes that emit traces receive the telemetry writer role."
  }

  assert {
    condition = alltrue([
      google_monitoring_alert_policy.api_server_errors.severity == "ERROR",
      google_monitoring_alert_policy.api_latency.severity == "WARNING",
      google_monitoring_alert_policy.access_rate_limit_cache_fallback.severity == "WARNING",
      length(google_monitoring_alert_policy.access_rate_limit_cache_fallback.conditions[0].condition_matched_log) == 1,
      strcontains(google_monitoring_alert_policy.access_rate_limit_cache_fallback.conditions[0].condition_matched_log[0].filter, "jsonPayload.event=\"access_rate_limit_cache_fallback\""),
      google_monitoring_alert_policy.access_rate_limit_cache_fallback.alert_strategy[0].notification_rate_limit[0].period == "900s",
      google_monitoring_alert_policy.job_failures.severity == "ERROR",
      google_monitoring_alert_policy.outbox_relay_failures.severity == "ERROR",
      length(google_monitoring_alert_policy.outbox_relay_failures.conditions) == 2,
      google_monitoring_alert_policy.outbox_relay_saturation.severity == "WARNING",
      length(google_monitoring_alert_policy.outbox_relay_saturation.conditions) == 1,
      length(google_monitoring_alert_policy.outbox_relay_saturation.conditions[0].condition_matched_log) == 1,
      strcontains(google_monitoring_alert_policy.outbox_relay_saturation.conditions[0].condition_matched_log[0].filter, "jsonPayload.event=\"outbox_relay_batch_saturated\""),
      google_monitoring_alert_policy.outbox_relay_saturation.alert_strategy[0].notification_rate_limit[0].period == "900s",
      google_monitoring_alert_policy.media_task_backlog.severity == "WARNING",
      google_monitoring_alert_policy.media_task_backlog.conditions[0].condition_threshold[0].threshold_value == 0,
      google_monitoring_alert_policy.media_task_backlog.conditions[0].condition_threshold[0].duration == "900s",
      google_monitoring_alert_policy.media_task_backlog.conditions[0].condition_threshold[0].aggregations[0].per_series_aligner == "ALIGN_MIN",
      strcontains(google_monitoring_alert_policy.media_task_backlog.conditions[0].condition_threshold[0].filter, "cloudtasks.googleapis.com/queue/depth"),
      length(google_monitoring_alert_policy.media_task_backlog.alert_strategy[0].notification_rate_limit) == 0,
      google_monitoring_alert_policy.media_task_failures.severity == "ERROR",
      google_monitoring_alert_policy.media_task_failures.conditions[0].condition_threshold[0].duration == "0s",
      length(google_monitoring_alert_policy.media_task_failures.alert_strategy[0].notification_rate_limit) == 0,
      strcontains(google_monitoring_alert_policy.media_task_failures.conditions[0].condition_threshold[0].filter, "cloudtasks.googleapis.com/queue/task_attempt_count"),
      strcontains(google_monitoring_alert_policy.media_task_failures.conditions[0].condition_threshold[0].filter, "response_code!=\"ok\""),
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
    job_runtime = {
      container_image = "asia-northeast3-docker.pkg.dev/seqret-staging/backend/job@sha256:2222222222222222222222222222222222222222222222222222222222222222"
      command         = ["python", "-m", "app.entrypoints.media_jobs"]
    }
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.worker.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY",
      google_cloud_run_v2_service.worker.template[0].containers[0].image == var.container_image,
      google_cloud_run_v2_job.job[0].template[0].template[0].containers[0].image == var.job_runtime.container_image,
      google_cloud_run_v2_service.worker.deletion_protection,
      google_cloud_run_v2_job.job[0].deletion_protection,
      length([
        for env in google_cloud_run_v2_service.worker.template[0].containers[0].env : env
        if env.name == "SEQRET_MEDIA_RETENTION_DAYS"
      ]) == 0,
      length([
        for env in google_cloud_run_v2_job.job[0].template[0].template[0].containers[0].env : env
        if env.name == "SEQRET_MEDIA_RETENTION_DAYS"
      ]) == 0,
    ])
    error_message = "B-owned runtimes must keep their explicit images and exclude A-owned settings."
  }
}

run "redis_secret_without_vpc_is_rejected" {
  command = plan

  variables {
    redis_url_secret_id = "seqret-redis-url"
  }

  expect_failures = [var.redis_vpc_network]
}

run "redis_vpc_without_secret_is_rejected" {
  command = plan

  variables {
    redis_vpc_network    = "projects/seqret-staging/global/networks/seqret-stg"
    redis_vpc_subnetwork = "projects/seqret-staging/regions/asia-northeast3/subnetworks/seqret-stg-run"
  }

  expect_failures = [var.redis_vpc_network]
}

run "redis_direct_vpc_is_exact" {
  command = plan

  variables {
    redis_url_secret_id  = "seqret-redis-url"
    redis_vpc_network    = "projects/seqret-staging/global/networks/seqret-stg"
    redis_vpc_subnetwork = "projects/seqret-staging/regions/asia-northeast3/subnetworks/seqret-stg-run"
  }

  assert {
    condition = alltrue([
      length(google_cloud_run_v2_service.api.template[0].vpc_access) == 1,
      google_cloud_run_v2_service.api.template[0].vpc_access[0].egress == "PRIVATE_RANGES_ONLY",
      length(google_cloud_run_v2_service.api.template[0].vpc_access[0].network_interfaces) == 1,
      google_cloud_run_v2_service.api.template[0].vpc_access[0].network_interfaces[0].network == var.redis_vpc_network,
      google_cloud_run_v2_service.api.template[0].vpc_access[0].network_interfaces[0].subnetwork == var.redis_vpc_subnetwork,
    ])
    error_message = "Redis must add exactly one private-ranges-only Direct VPC interface to the API."
  }
}

run "redis_network_without_subnetwork_is_rejected" {
  command = plan

  variables {
    redis_vpc_network = "projects/seqret-staging/global/networks/seqret-stg"
  }

  expect_failures = [var.redis_vpc_network]
}

run "redis_subnetwork_without_network_is_rejected" {
  command = plan

  variables {
    redis_vpc_subnetwork = "projects/seqret-staging/regions/asia-northeast3/subnetworks/seqret-stg-run"
  }

  expect_failures = [var.redis_vpc_network]
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

run "numeric_frontend_origin_is_rejected" {
  command = plan

  variables {
    frontend_origin = "https://127.1"
  }

  expect_failures = [var.frontend_origin]
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

run "invalid_media_retention_is_rejected" {
  command = plan

  variables {
    media_retention_days = 0
  }

  expect_failures = [var.media_retention_days]
}

run "oversized_cloud_sql_socket_is_rejected" {
  command = plan

  variables {
    cloud_sql_instance_id = "seqret-staging-database-instance-with-a-name-that-exceeds-the-linux-unix-socket-path-limit"
  }

  expect_failures = [var.cloud_sql_instance_id]
}
