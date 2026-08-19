locals {
  environment_abbreviation = {
    staging    = "stg"
    production = "prd"
  }[var.environment]

  resource_stem                      = "${var.name_prefix}-${local.environment_abbreviation}"
  api_name                           = "${local.resource_stem}-api"
  worker_name                        = "${local.resource_stem}-worker"
  task_queue_name                    = "${local.resource_stem}-media"
  task_invoker_name                  = "${local.resource_stem}-tasks"
  cloud_tasks_service_agent          = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
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
    SEQRET_DATABASE_MAX_OVERFLOW                 = "1"
    SEQRET_DATABASE_POOL_SIZE                    = "2"
    SEQRET_MEDIA_RETENTION_DAYS                  = tostring(var.media_retention_days)
    SEQRET_FRONTEND_ORIGIN                       = var.frontend_origin
    SEQRET_MEDIA_BUCKET_NAME                     = var.media_bucket_name
    SEQRET_STORAGE_SIGNING_SERVICE_ACCOUNT_EMAIL = google_service_account.api.email
  })

  outbox_notification_environment = var.notification_delivery_enabled ? {
    SEQRET_FRONTEND_ORIGIN                       = var.frontend_origin
    SEQRET_NOTIFICATION_DELIVERY_ENABLED         = "true"
    SEQRET_NHN_NOTIFICATION_EMAIL_APP_KEY        = var.nhn_notification_email_app_key
    SEQRET_NHN_NOTIFICATION_EMAIL_SENDER_ADDRESS = var.nhn_notification_email_sender_address
    SEQRET_NHN_NOTIFICATION_EMAIL_SENDER_NAME    = var.nhn_notification_email_sender_name
    SEQRET_NHN_NOTIFICATION_SMS_APP_KEY          = var.nhn_notification_sms_app_key
    SEQRET_NHN_NOTIFICATION_SMS_SENDER_NUMBER    = var.nhn_notification_sms_sender_number
    SEQRET_NHN_NOTIFICATION_KAKAO_APP_KEY        = var.nhn_notification_kakao_app_key
    SEQRET_NHN_NOTIFICATION_KAKAO_SENDER_KEY     = var.nhn_notification_kakao_sender_key
    SEQRET_NHN_NOTIFICATION_KAKAO_TEMPLATE_CODE  = var.nhn_notification_kakao_template_code
    } : {
    SEQRET_NOTIFICATION_DELIVERY_ENABLED = "false"
  }

  outbox_relay_environment = merge(local.observed_runtime_environment, local.outbox_notification_environment, {
    SEQRET_DATABASE_MAX_OVERFLOW              = "0"
    SEQRET_DATABASE_POOL_SIZE                 = "1"
    SEQRET_PUBSUB_PROJECT_ID                  = var.project_id
    SEQRET_PUBSUB_TOPIC_ID                    = local.events_topic_name
    SEQRET_TASK_QUEUE_LOCATION                = var.region
    SEQRET_TASK_QUEUE_NAME                    = local.task_queue_name
    SEQRET_TASK_WORKER_URL                    = google_cloud_run_v2_service.worker.uri
    SEQRET_TASK_INVOKER_SERVICE_ACCOUNT_EMAIL = google_service_account.task_invoker.email
  })

  worker_environment = merge(local.observed_runtime_environment, {
    SEQRET_DATABASE_MAX_OVERFLOW = "0"
    SEQRET_DATABASE_POOL_SIZE    = "1"
    SEQRET_ANALYSIS_LOCATION     = var.region
    SEQRET_MEDIA_BUCKET_NAME     = var.media_bucket_name
  })

  api_secret_environment = merge(
    { SEQRET_DATABASE_URL = var.api_database_url_secret_id },
    var.redis_url_secret_id == null ? {} : { SEQRET_REDIS_URL = var.redis_url_secret_id },
  )

  outbox_notification_secret_environment = var.notification_delivery_enabled ? {
    SEQRET_NHN_NOTIFICATION_EMAIL_SECRET_KEY = var.nhn_notification_email_secret_key_secret_id
    SEQRET_NHN_NOTIFICATION_SMS_SECRET_KEY   = var.nhn_notification_sms_secret_key_secret_id
    SEQRET_NHN_NOTIFICATION_KAKAO_SECRET_KEY = var.nhn_notification_kakao_secret_key_secret_id
  } : {}
}

check "notification_delivery_configuration" {
  assert {
    condition = !var.notification_delivery_enabled || alltrue([
      var.nhn_notification_email_app_key != null && try(length(trimspace(var.nhn_notification_email_app_key)) > 0, false),
      var.nhn_notification_email_sender_address != null && try(length(trimspace(var.nhn_notification_email_sender_address)) > 0, false),
      var.nhn_notification_email_sender_name != null && try(length(trimspace(var.nhn_notification_email_sender_name)) > 0, false),
      var.nhn_notification_email_secret_key_secret_id != null && try(length(trimspace(var.nhn_notification_email_secret_key_secret_id)) > 0, false),
      var.nhn_notification_sms_app_key != null && try(length(trimspace(var.nhn_notification_sms_app_key)) > 0, false),
      var.nhn_notification_sms_sender_number != null && try(length(trimspace(var.nhn_notification_sms_sender_number)) > 0, false),
      var.nhn_notification_sms_secret_key_secret_id != null && try(length(trimspace(var.nhn_notification_sms_secret_key_secret_id)) > 0, false),
      var.nhn_notification_kakao_app_key != null && try(length(trimspace(var.nhn_notification_kakao_app_key)) > 0, false),
      var.nhn_notification_kakao_sender_key != null && try(length(trimspace(var.nhn_notification_kakao_sender_key)) > 0, false),
      var.nhn_notification_kakao_template_code != null && try(length(trimspace(var.nhn_notification_kakao_template_code)) > 0, false),
      var.nhn_notification_kakao_secret_key_secret_id != null && try(length(trimspace(var.nhn_notification_kakao_secret_key_secret_id)) > 0, false),
    ])
    error_message = "Enabled notification delivery requires all NHN app, sender, template, and Secret Manager IDs."
  }
}

check "database_secret_ids_are_distinct" {
  assert {
    condition = length(toset([
      var.migration_database_url_secret_id,
      var.api_database_url_secret_id,
      var.worker_database_url_secret_id,
      var.outbox_relay_database_url_secret_id,
    ])) == 4
    error_message = "Migration, API, worker, and Outbox relay database secret IDs must be distinct."
  }
}

check "notification_secret_ids_are_distinct" {
  assert {
    condition = !var.notification_delivery_enabled || length(toset(compact([
      var.migration_database_url_secret_id,
      var.api_database_url_secret_id,
      var.worker_database_url_secret_id,
      var.outbox_relay_database_url_secret_id,
      var.nhn_notification_email_secret_key_secret_id,
      var.nhn_notification_sms_secret_key_secret_id,
      var.nhn_notification_kakao_secret_key_secret_id,
    ]))) == 7
    error_message = "Database and enabled NHN notification Secret Manager IDs must be distinct."
  }
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
      length(local.task_invoker_name) <= 30,
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
