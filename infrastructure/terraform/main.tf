resource "google_project_service" "required" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
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

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = local.worker_name
  display_name = "SEQRET ${var.environment} private worker runtime"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "job" {
  project      = var.project_id
  account_id   = local.job_name
  display_name = "SEQRET ${var.environment} Cloud Run Job runtime"

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "api" {
  project             = var.project_id
  name                = local.api_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  deletion_protection = var.deletion_protection
  labels              = local.common_labels

  template {
    service_account = google_service_account.api.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }

    containers {
      image   = var.container_image
      command = length(var.api_command) == 0 ? null : var.api_command
      args    = length(var.api_args) == 0 ? null : var.api_args

      ports {
        name           = "http1"
        container_port = var.container_port
      }

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
          memory = "512Mi"
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "worker" {
  project             = var.project_id
  name                = local.worker_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = var.deletion_protection
  labels              = local.common_labels

  template {
    service_account = google_service_account.worker.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.worker_max_instances
    }

    containers {
      image   = var.container_image
      command = length(var.worker_command) == 0 ? null : var.worker_command
      args    = length(var.worker_args) == 0 ? null : var.worker_args

      ports {
        name           = "http1"
        container_port = var.container_port
      }

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

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_job" "job" {
  project             = var.project_id
  name                = local.job_name
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.common_labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.job.email
      max_retries     = var.job_max_retries
      timeout         = var.job_timeout

      containers {
        image   = var.container_image
        command = length(var.job_command) == 0 ? null : var.job_command
        args    = length(var.job_args) == 0 ? null : var.job_args

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
