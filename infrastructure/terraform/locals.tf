locals {
  environment_abbreviation = {
    staging    = "stg"
    production = "prd"
  }[var.environment]

  resource_stem = "${var.name_prefix}-${local.environment_abbreviation}"
  api_name      = "${local.resource_stem}-api"
  worker_name   = "${local.resource_stem}-worker"
  job_name      = "${local.resource_stem}-job"

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
}

check "cloud_run_name_lengths" {
  assert {
    condition = alltrue([
      length(local.api_name) <= 49,
      length(local.worker_name) <= 49,
      length(local.job_name) <= 49,
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
    ])
    error_message = "Runtime service account IDs must not exceed 30 characters."
  }
}
