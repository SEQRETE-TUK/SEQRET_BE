resource "google_project_service" "required" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtasks.googleapis.com",
    "compute.googleapis.com",
    "containerscanning.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = local.api_name
  display_name = "SEQRET ${var.environment} API runtime"

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "api_media_object_creator" {
  bucket = var.media_bucket_name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_bucket_iam_member" "api_media_object_viewer" {
  bucket = var.media_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_service_account_iam_member" "api_self_token_creator" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${local.api_name}@${var.project_id}.iam.gserviceaccount.com"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.api.email}"
}

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = local.worker_name
  display_name = "SEQRET ${var.environment} private worker runtime"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "task_invoker" {
  project      = var.project_id
  account_id   = local.task_invoker_name
  display_name = "SEQRET ${var.environment} private worker caller"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "worker_cloud_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_trace_writer" {
  project = var.project_id
  role    = "roles/telemetry.tracesWriter"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_telemetry_consumer" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_database" {
  project   = var.project_id
  secret_id = var.worker_database_url_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_storage_bucket_iam_member" "worker_media_objects" {
  bucket = var.media_bucket_name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_service_account" "job" {
  project      = var.project_id
  account_id   = local.job_name
  display_name = "SEQRET ${var.environment} Cloud Run Job runtime"

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "api" {
  project              = var.project_id
  name                 = local.api_name
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  deletion_protection  = var.deletion_protection
  labels               = local.common_labels
  invoker_iam_disabled = true
  default_uri_disabled = true
  launch_stage         = "BETA"

  scaling {
    max_instance_count = var.api_max_instances
  }

  template {
    labels                           = merge(local.common_labels, { readiness_contract = "v1" })
    service_account                  = google_service_account.api.email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    max_instance_request_concurrency = 3

    dynamic "vpc_access" {
      for_each = var.redis_url_secret_id != null && var.redis_vpc_network != null ? [true] : []
      content {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = var.redis_vpc_network
          subnetwork = var.redis_vpc_subnetwork
        }
      }
    }

    containers {
      name    = "api"
      image   = var.container_image
      command = length(var.api_command) == 0 ? null : var.api_command
      args    = length(var.api_args) == 0 ? null : var.api_args

      ports {
        name           = "http1"
        container_port = 8080
      }

      dynamic "env" {
        for_each = local.api_runtime_environment
        content {
          name  = env.key
          value = env.value
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      dynamic "env" {
        for_each = local.api_secret_environment
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 3
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
        }
      }

      liveness_probe {
        initial_delay_seconds = 10
        timeout_seconds       = 3
        period_seconds        = 10
        failure_threshold     = 3
        http_get {
          path = "/healthz"
          port = 8080
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

  dynamic "traffic" {
    for_each = var.stable_api_revision == null ? [] : [var.stable_api_revision]
    content {
      type     = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
      revision = traffic.value
      percent  = 100
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = var.stable_api_revision == null ? 100 : 0
  }

  depends_on = [
    google_project_service.required,
    google_project_service.observability,
    google_project_iam_member.api_trace_writer,
    google_project_iam_member.api_telemetry_consumer,
    google_project_iam_member.api_cloud_sql_client,
    google_service_account_iam_member.api_self_token_creator,
    google_secret_manager_secret_iam_member.api_database,
    google_secret_manager_secret_iam_member.api_redis,
    google_storage_bucket_iam_member.api_media_object_creator,
    google_storage_bucket_iam_member.api_media_object_viewer,
  ]
}

resource "google_service_account" "migration" {
  project      = var.project_id
  account_id   = local.migration_name
  display_name = "SEQRET ${var.environment} schema migration runtime"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "api_cloud_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "migration_cloud_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.migration.email}"
}

resource "google_artifact_registry_repository" "backend" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository_id
  description   = "Digest-pinned SEQRET backend container images"
  format        = "DOCKER"
  mode          = "STANDARD_REPOSITORY"
  labels        = local.common_labels

  cleanup_policy_dry_run = true

  docker_config { immutable_tags = false }

  cleanup_policies {
    id     = "delete-older-than-90-days"
    action = "DELETE"

    condition {
      older_than = "7776000s"
    }
  }

  cleanup_policies {
    id     = "keep-most-recent-50"
    action = "KEEP"

    most_recent_versions {
      keep_count = 50
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_job" "migration" {
  project             = var.project_id
  name                = local.migration_name
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.common_labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.migration.email
      max_retries     = 0
      timeout         = var.migration_timeout

      containers {
        name    = "migration"
        image   = var.container_image
        command = ["python", "-m", "app.entrypoints.migrate"]

        dynamic "env" {
          for_each = local.observed_runtime_environment
          content {
            name  = env.key
            value = env.value
          }
        }

        env {
          name = "SEQRET_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = var.migration_database_url_secret_id
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
    google_project_iam_member.migration_trace_writer,
    google_project_iam_member.migration_telemetry_consumer,
    google_project_iam_member.migration_cloud_sql_client,
    google_secret_manager_secret_iam_member.migration_database,
  ]
}

resource "google_cloud_run_v2_service" "worker" {
  project             = var.project_id
  name                = local.worker_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = var.deletion_protection
  labels              = local.common_labels

  lifecycle {
    prevent_destroy = true
  }

  template {
    service_account                  = google_service_account.worker.email
    timeout                          = "900s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = var.worker_max_instances
    }

    containers {
      image   = var.container_image
      command = ["python", "-m", "uvicorn", "app.entrypoints.worker:app"]
      args    = ["--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]

      ports {
        name           = "http1"
        container_port = 8080
      }

      dynamic "env" {
        for_each = local.worker_environment
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "SEQRET_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = var.worker_database_url_secret_id
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
          memory = "1Gi"
        }
      }

      startup_probe {
        initial_delay_seconds = 1
        timeout_seconds       = 3
        period_seconds        = 3
        failure_threshold     = 10
        http_get {
          path = "/healthz"
          port = 8080
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

  depends_on = [
    google_project_service.required,
    google_project_service.observability,
    google_project_iam_member.worker_cloud_sql_client,
    google_project_iam_member.worker_vertex_ai_user,
    google_project_iam_member.worker_trace_writer,
    google_project_iam_member.worker_telemetry_consumer,
    google_secret_manager_secret_iam_member.worker_database,
    google_storage_bucket_iam_member.worker_media_objects,
  ]
}

resource "google_cloud_run_v2_job" "job" {
  count = var.job_runtime == null ? 0 : 1

  project             = var.project_id
  name                = local.job_name
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.common_labels

  lifecycle {
    prevent_destroy = true
  }

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.job.email
      max_retries     = var.job_max_retries
      timeout         = var.job_timeout

      containers {
        image   = var.job_runtime.container_image
        command = var.job_runtime.command
        args    = var.job_runtime.args

        dynamic "env" {
          for_each = local.runtime_environment
          content {
            name  = env.key
            value = env.value
          }
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

moved {
  from = google_cloud_run_v2_service.worker[0]
  to   = google_cloud_run_v2_service.worker
}

moved {
  from = google_cloud_run_v2_job.job
  to   = google_cloud_run_v2_job.job[0]
}
