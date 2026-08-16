resource "google_project_service" "observability" {
  for_each = toset([
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "telemetry.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_project_iam_member" "api_trace_writer" {
  project = var.project_id
  role    = "roles/telemetry.tracesWriter"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "migration_trace_writer" {
  project = var.project_id
  role    = "roles/telemetry.tracesWriter"
  member  = "serviceAccount:${google_service_account.migration.email}"
}

resource "google_project_iam_member" "api_telemetry_consumer" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "migration_telemetry_consumer" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.migration.email}"
}

resource "google_secret_manager_secret_iam_member" "api_database" {
  project   = var.project_id
  secret_id = var.database_url_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "migration_database" {
  project   = var.project_id
  secret_id = var.database_url_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migration.email}"
}

resource "google_secret_manager_secret_iam_member" "api_redis" {
  count = var.redis_url_secret_id == null ? 0 : 1

  project   = var.project_id
  secret_id = var.redis_url_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_monitoring_alert_policy" "api_server_errors" {
  project               = var.project_id
  display_name          = "${local.api_name} server errors"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "ERROR"
  user_labels           = local.common_labels

  conditions {
    display_name = "At least one API 5xx response in five minutes"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.label.service_name=\"${local.api_name}\"",
        "metric.type=\"run.googleapis.com/request_count\"",
        "metric.label.response_code_class=\"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  alert_strategy { auto_close = "1800s" }
  documentation {
    subject   = "${local.api_name} is returning server errors"
    content   = "Inspect correlated Cloud Run JSON logs and traces. If this began after a rollout, run the rollback workflow with the previous revision."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "api_latency" {
  project               = var.project_id
  display_name          = "${local.api_name} high p95 latency"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "WARNING"
  user_labels           = local.common_labels

  conditions {
    display_name = "API p95 latency exceeds the objective"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.label.service_name=\"${local.api_name}\"",
        "metric.type=\"run.googleapis.com/request_latencies\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.api_p95_latency_threshold_ms
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_PERCENTILE_95"
      }
    }
  }

  alert_strategy { auto_close = "1800s" }
  documentation {
    subject   = "${local.api_name} p95 latency is above target"
    content   = "Inspect request traces and Cloud Run metrics. Roll back when the increase is isolated to the latest revision."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "cloud_sql_connections" {
  count = var.database_connection_alert_threshold == null ? 0 : 1

  project               = var.project_id
  display_name          = "${var.cloud_sql_instance_id} connection pressure"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "WARNING"
  user_labels           = local.common_labels

  conditions {
    display_name = "PostgreSQL backends exceed the approved connection budget"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloudsql_database\"",
        "resource.label.database_id=\"${var.project_id}:${var.cloud_sql_instance_id}\"",
        "metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = coalesce(var.database_connection_alert_threshold, 1)
      duration        = "120s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.database_id"]
      }
    }
  }

  alert_strategy { auto_close = "1800s" }
  documentation {
    subject   = "${var.cloud_sql_instance_id} is nearing its PostgreSQL connection ceiling"
    content   = "The policy sums num_backends across every database on the instance. Inspect Cloud Run revision counts and pool settings before raising max_connections or scaling runtimes."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "access_rate_limit_cache_fallback" {
  project               = var.project_id
  display_name          = "${local.api_name} Redis rate-limit fallback"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "WARNING"
  user_labels           = local.common_labels

  conditions {
    display_name = "API used the database rate limit after a Redis failure"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${local.api_name}\"",
        "resource.labels.location=\"${var.region}\"",
        "jsonPayload.event=\"access_rate_limit_cache_fallback\"",
      ])
    }
  }

  alert_strategy {
    auto_close = "1800s"
    notification_rate_limit {
      period = "900s"
    }
  }
  documentation {
    subject   = "${local.api_name} is using the database rate-limit fallback"
    content   = "The database limit remains active. Inspect the Redis secret, Direct VPC path, and Memorystore availability before changing traffic."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "job_failures" {
  project               = var.project_id
  display_name          = "${local.job_name} execution failures"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "ERROR"
  user_labels           = local.common_labels

  conditions {
    display_name = "At least one failed job execution"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_job\"",
        "resource.label.job_name=\"${local.job_name}\"",
        "metric.type=\"run.googleapis.com/job/completed_execution_count\"",
        "metric.label.result=\"failed\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  alert_strategy { auto_close = "1800s" }
  documentation {
    subject   = "${local.job_name} execution failed"
    content   = "Inspect the failed Cloud Run Job execution and its structured logs before retrying."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "outbox_relay_failures" {
  project               = var.project_id
  display_name          = "${local.outbox_relay_name} execution health"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "ERROR"
  user_labels           = local.common_labels

  conditions {
    display_name = "At least one failed Outbox relay execution"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_job\"",
        "resource.label.job_name=\"${local.outbox_relay_name}\"",
        "metric.type=\"run.googleapis.com/job/completed_execution_count\"",
        "metric.label.result=\"failed\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  conditions {
    display_name = "No completed Outbox relay execution for ten minutes"
    condition_absent {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_job\"",
        "resource.label.job_name=\"${local.outbox_relay_name}\"",
        "metric.type=\"run.googleapis.com/job/completed_execution_count\"",
      ])
      duration = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  alert_strategy { auto_close = "1800s" }
  documentation {
    subject   = "${local.outbox_relay_name} is failing or not running"
    content   = "Inspect the scheduler and relay executions. Database leases make a later scheduled execution safe."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "outbox_relay_saturation" {
  project               = var.project_id
  display_name          = "${local.outbox_relay_name} batch saturation"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "WARNING"
  user_labels           = local.common_labels

  conditions {
    display_name = "Outbox relay reached its configured batch limit"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_job\"",
        "resource.labels.job_name=\"${local.outbox_relay_name}\"",
        "resource.labels.location=\"${var.region}\"",
        "jsonPayload.event=\"outbox_relay_batch_saturated\"",
      ])
    }
  }

  alert_strategy {
    auto_close = "1800s"
    notification_rate_limit {
      period = "900s"
    }
  }
  documentation {
    subject   = "${local.outbox_relay_name} reached its batch limit"
    content   = "A successful execution claimed the configured maximum batch. Inspect repeated executions before increasing capacity; one saturated batch does not prove that rows remain."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "media_task_backlog" {
  project               = var.project_id
  display_name          = "${local.task_queue_name} sustained backlog"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "WARNING"
  user_labels           = local.common_labels

  conditions {
    display_name = "Media tasks remain queued for fifteen minutes"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloud_tasks_queue\"",
        "resource.label.queue_id=\"${local.task_queue_name}\"",
        "resource.label.location=\"${var.region}\"",
        "metric.type=\"cloudtasks.googleapis.com/queue/depth\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "900s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MIN"
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }
  documentation {
    subject   = "${local.task_queue_name} has a sustained backlog"
    content   = "Inspect Cloud Tasks attempt logs, the private worker revision, and worker database or storage errors before changing queue capacity."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "media_task_failures" {
  project               = var.project_id
  display_name          = "${local.task_queue_name} failed attempts"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "ERROR"
  user_labels           = local.common_labels

  conditions {
    display_name = "Media task attempt failed"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloud_tasks_queue\"",
        "resource.label.queue_id=\"${local.task_queue_name}\"",
        "resource.label.location=\"${var.region}\"",
        "metric.type=\"cloudtasks.googleapis.com/queue/task_attempt_count\"",
        "metric.label.response_code!=\"ok\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }
  documentation {
    subject   = "${local.task_queue_name} has a failed attempt"
    content   = "Inspect the Cloud Tasks response code and private worker logs. Retry a failed background job only after its execution lease expires."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "api_uptime" {
  project               = var.project_id
  display_name          = "${local.api_name} external uptime"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "ERROR"
  user_labels           = local.common_labels

  conditions {
    display_name = "API failed external HTTPS health checks"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"uptime_url\"",
        "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\"",
        "metric.label.check_id=\"${google_monitoring_uptime_check_config.api.uptime_check_id}\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "120s"
      aggregations {
        alignment_period     = "120s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
      }
    }
  }

  alert_strategy { auto_close = "1800s" }
  documentation {
    subject   = "${local.api_name} is unavailable through the public edge"
    content   = "Inspect the HTTPS load balancer, Cloud Armor logs, and the active Cloud Run revision. Restore the previous ready revision when the failure began after deployment."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_custom_service" "api" {
  project      = var.project_id
  service_id   = local.api_name
  display_name = "${local.api_name} public API"
  user_labels  = local.common_labels

  depends_on = [google_project_service.observability]
}

resource "google_monitoring_slo" "api_availability" {
  project             = var.project_id
  service             = google_monitoring_custom_service.api.service_id
  slo_id              = "availability"
  display_name        = "99% API request availability over 30 days"
  goal                = 0.99
  rolling_period_days = 30
  user_labels         = local.common_labels

  request_based_sli {
    good_total_ratio {
      bad_service_filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.label.service_name=\"${local.api_name}\"",
        "metric.type=\"run.googleapis.com/request_count\"",
        "metric.label.response_code_class=\"5xx\"",
      ])
      total_service_filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.label.service_name=\"${local.api_name}\"",
        "metric.type=\"run.googleapis.com/request_count\"",
      ])
    }
  }
}

resource "google_monitoring_dashboard" "runtime" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "SEQRET ${var.environment} runtime"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          width = 6, height = 4
          widget = {
            title = "API request rate by response class"
            xyChart = {
              dataSets = [{
                plotType = "LINE"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter = "resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.api_name}\" AND metric.type=\"run.googleapis.com/request_count\""
                  aggregation = {
                    alignmentPeriod    = "60s"
                    perSeriesAligner   = "ALIGN_RATE"
                    crossSeriesReducer = "REDUCE_SUM"
                    groupByFields      = ["metric.label.response_code_class"]
                  }
                } }
              }]
              yAxis = { scale = "LINEAR" }
            }
          }
        },
        {
          xPos = 6, width = 6, height = 4
          widget = {
            title = "API p95 request latency"
            xyChart = {
              dataSets = [{
                plotType = "LINE"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter = "resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.api_name}\" AND metric.type=\"run.googleapis.com/request_latencies\""
                  aggregation = {
                    alignmentPeriod  = "300s"
                    perSeriesAligner = "ALIGN_PERCENTILE_95"
                  }
                } }
              }]
              thresholds = [{ value = var.api_p95_latency_threshold_ms }]
              yAxis      = { scale = "LINEAR" }
            }
          }
        },
        {
          yPos = 4, width = 6, height = 4
          widget = {
            title = "Cloud Run job executions by result"
            xyChart = {
              dataSets = [{
                plotType = "STACKED_BAR"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter = "resource.type=\"cloud_run_job\" AND resource.label.job_name=\"${local.job_name}\" AND metric.type=\"run.googleapis.com/job/completed_execution_count\""
                  aggregation = {
                    alignmentPeriod    = "300s"
                    perSeriesAligner   = "ALIGN_SUM"
                    crossSeriesReducer = "REDUCE_SUM"
                    groupByFields      = ["metric.label.result"]
                  }
                } }
              }]
              yAxis = { scale = "LINEAR" }
            }
          }
        }
      ]
    }
  })
  depends_on = [google_project_service.observability]
}
