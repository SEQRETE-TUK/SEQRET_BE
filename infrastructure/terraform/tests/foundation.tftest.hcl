mock_provider "google" {}

variables {
  project_id      = "seqret-staging"
  region          = "asia-northeast3"
  environment     = "staging"
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
    condition     = google_cloud_run_v2_service.worker.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    error_message = "The worker must remain internal-only."
  }

  assert {
    condition = length(toset([
      google_service_account.api.account_id,
      google_service_account.worker.account_id,
      google_service_account.job.account_id,
    ])) == 3
    error_message = "API, worker and job must use distinct service accounts."
  }

  assert {
    condition = (
      toset(keys(google_project_service.required)) ==
      toset([
        "artifactregistry.googleapis.com",
        "iam.googleapis.com",
        "run.googleapis.com",
      ])
    )
    error_message = "The runtime foundation must enable its required Google Cloud APIs."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.api.deletion_protection,
      google_cloud_run_v2_service.worker.deletion_protection,
      google_cloud_run_v2_job.job.deletion_protection,
    ])
    error_message = "Deletion protection must default to enabled."
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
      google_cloud_run_v2_service.worker.name == "seqret-prd-worker",
      google_cloud_run_v2_job.job.name == "seqret-prd-job",
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
