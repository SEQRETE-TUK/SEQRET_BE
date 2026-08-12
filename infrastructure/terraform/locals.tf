locals {
  environment_abbreviation = {
    staging    = "stg"
    production = "prd"
  }[var.environment]

  resource_stem                      = "${var.name_prefix}-${local.environment_abbreviation}"
  api_name                           = "${local.resource_stem}-api"
  worker_name                        = "${local.resource_stem}-worker"
  job_name                           = "${local.resource_stem}-job"
  migration_name                     = "${local.resource_stem}-migrate"
  outbox_relay_name                  = "${local.resource_stem}-relay"
  outbox_scheduler_name              = "${local.resource_stem}-cron"
  events_topic_name                  = "${local.resource_stem}-events"
  cloud_sql_instance_connection_name = "${var.project_id}:${var.region}:${var.cloud_sql_instance_id}"

  common_labels = merge(
    var.labels,
    {
      application = var.name_prefix
      environment = var.environment
      managed_by  = "terraform"
    },
  )

  runtime_environment = {
    SEQRET_DEBUG        = "false"
    SEQRET_ENVIRONMENT  = var.environment
    SEQRET_LOG_LEVEL    = "INFO"
    SEQRET_SERVICE_NAME = var.service_name
  }

  observed_runtime_environment = merge(local.runtime_environment, {
    SEQRET_DATABASE_SOCKET_PATH               = "/cloudsql/${local.cloud_sql_instance_connection_name}"
    SEQRET_GCP_PROJECT_ID                     = var.project_id
    SEQRET_OTEL_ENABLED                       = "true"
    SEQRET_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = "https://telemetry.googleapis.com:443/v1/traces"
    SEQRET_OTEL_TRACE_SAMPLE_RATIO            = tostring(var.otel_trace_sample_ratio)
  })

  api_runtime_environment = merge(local.observed_runtime_environment, {
    SEQRET_DATABASE_MAX_OVERFLOW = "1"
    SEQRET_DATABASE_POOL_SIZE    = "2"
    SEQRET_MEDIA_RETENTION_DAYS  = tostring(var.media_retention_days)
  })

  outbox_relay_environment = merge(local.observed_runtime_environment, {
    SEQRET_DATABASE_MAX_OVERFLOW = "0"
    SEQRET_DATABASE_POOL_SIZE    = "1"
    SEQRET_PUBSUB_PROJECT_ID     = var.project_id
    SEQRET_PUBSUB_TOPIC_ID       = local.events_topic_name
  })

  api_secret_environment = merge(
    { SEQRET_DATABASE_URL = var.database_url_secret_id },
    var.redis_url_secret_id == null ? {} : { SEQRET_REDIS_URL = var.redis_url_secret_id },
  )
}

check "cloud_run_name_lengths" {
  assert {
    condition = alltrue([
      length(local.api_name) <= 49,
      length(local.worker_name) <= 49,
      length(local.job_name) <= 49,
      length(local.migration_name) <= 49,
      length(local.outbox_relay_name) <= 49,
    ])
    error_message = "Cloud Run resource names must not exceed 49 characters."
  }
}

check "service_account_name_lengths" {
  assert {
    condition = alltrue([
      length(local.api_name) <= 30,
      length(local.worker_name) <= 30,
      length(local.job_name) <= 30,
      length(local.migration_name) <= 30,
      length(local.outbox_relay_name) <= 30,
      length(local.outbox_scheduler_name) <= 30,
    ])
    error_message = "Runtime service account IDs must not exceed 30 characters."
  }
}

check "stable_api_revision_belongs_to_service" {
  assert {
    condition = (
      var.stable_api_revision == null ||
      startswith(var.stable_api_revision, "${local.api_name}-")
    )
    error_message = "stable_api_revision must belong to the API service in the selected environment."
  }
}
