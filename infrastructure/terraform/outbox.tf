resource "google_pubsub_topic" "events" {
  project                    = var.project_id
  name                       = local.events_topic_name
  labels                     = local.common_labels
  message_retention_duration = "2678400s"
  deletion_policy            = var.deletion_protection ? "PREVENT" : "DELETE"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "outbox_relay" {
  project      = var.project_id
  account_id   = local.outbox_relay_name
  display_name = "SEQRET ${var.environment} Outbox relay runtime"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "outbox_scheduler" {
  project      = var.project_id
  account_id   = local.outbox_scheduler_name
  display_name = "SEQRET ${var.environment} Outbox scheduler caller"

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic_iam_member" "outbox_relay_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.events.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.outbox_relay.email}"
}

resource "google_project_iam_member" "outbox_relay_cloud_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.outbox_relay.email}"
}

resource "google_project_iam_member" "outbox_relay_trace_writer" {
  project = var.project_id
  role    = "roles/telemetry.tracesWriter"
  member  = "serviceAccount:${google_service_account.outbox_relay.email}"
}

resource "google_project_iam_member" "outbox_relay_telemetry_consumer" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.outbox_relay.email}"
}

resource "google_secret_manager_secret_iam_member" "outbox_relay_database" {
  project   = var.project_id
  secret_id = var.database_url_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.outbox_relay.email}"
}

resource "google_cloud_run_v2_job" "outbox_relay" {
  project             = var.project_id
  name                = local.outbox_relay_name
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.common_labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.outbox_relay.email
      max_retries     = 0
      timeout         = "60s"

      containers {
        name    = "relay"
        image   = var.container_image
        command = ["python", "-m", "app.entrypoints.outbox_relay"]

        dynamic "env" {
          for_each = local.outbox_relay_environment
          content {
            name  = env.key
            value = env.value
          }
        }

        env {
          name = "SEQRET_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = var.database_url_secret_id
              version = "latest"
            }
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [local.cloud_sql_instance_connection_name]
        }
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_project_service.observability,
    google_project_iam_member.outbox_relay_cloud_sql_client,
    google_project_iam_member.outbox_relay_trace_writer,
    google_project_iam_member.outbox_relay_telemetry_consumer,
    google_pubsub_topic_iam_member.outbox_relay_publisher,
    google_secret_manager_secret_iam_member.outbox_relay_database,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "outbox_scheduler_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.outbox_relay.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.outbox_scheduler.email}"
}

resource "google_cloud_scheduler_job" "outbox_relay" {
  project          = var.project_id
  region           = var.region
  name             = local.outbox_scheduler_name
  description      = "Run the SEQRET Outbox relay every minute"
  deletion_policy  = var.deletion_protection ? "PREVENT" : "DELETE"
  schedule         = "* * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "30s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.outbox_relay.name}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }

    oauth_token {
      service_account_email = google_service_account.outbox_scheduler.email
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_job.outbox_relay,
    google_cloud_run_v2_job_iam_member.outbox_scheduler_invoker,
  ]
}
