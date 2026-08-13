variable "project_id" {
  description = "GCP project dedicated to one deployment environment."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid lowercase GCP project ID with 6 to 30 characters."
  }
}

variable "region" {
  description = "GCP region for Cloud Run resources."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
    error_message = "region must be a valid GCP region such as asia-northeast3."
  }
}

variable "environment" {
  description = "Isolated deployment environment."
  type        = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "name_prefix" {
  description = "Short lowercase prefix used for GCP resource names."
  type        = string
  default     = "seqret"

  validation {
    condition = (
      length(var.name_prefix) <= 19 &&
      can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.name_prefix))
    )
    error_message = "name_prefix must be a lowercase DNS label with at most 19 characters."
  }
}

variable "service_name" {
  description = "Shared application service name before runtime suffixes are appended."
  type        = string
  default     = "seqret"

  validation {
    condition = (
      length(var.service_name) <= 42 &&
      can(regex("^[a-z](?:[a-z0-9-]*[a-z0-9])?$", var.service_name))
    )
    error_message = "service_name must be a lowercase DNS label with at most 42 characters."
  }
}

variable "container_image" {
  description = "Immutable same-project Artifact Registry image reference."
  type        = string

  validation {
    condition = (
      startswith(
        var.container_image,
        "${var.region}-docker.pkg.dev/${var.project_id}/",
      ) &&
      can(regex(
        "^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$",
        trimprefix(
          var.container_image,
          "${var.region}-docker.pkg.dev/${var.project_id}/",
        ),
      ))
    )
    error_message = "container_image must be a same-region, same-project Artifact Registry path ending in @sha256:<64 lowercase hex>."
  }
}

variable "deletion_protection" {
  description = "Protect Cloud Run resources, the retained event topic, and its scheduled trigger from accidental Terraform deletion."
  type        = bool
  default     = true
}

variable "api_domain" {
  description = "Public DNS name routed to the API load balancer, without scheme or path."
  type        = string

  validation {
    condition = (
      length(var.api_domain) <= 253 &&
      can(regex(
        "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$",
        var.api_domain,
      ))
    )
    error_message = "api_domain must be a lowercase fully-qualified DNS name without scheme or path."
  }
}

variable "frontend_origin" {
  description = "Single HTTPS browser origin allowed to call the API."
  type        = string

  validation {
    condition     = can(regex("^https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\\.[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$", var.frontend_origin))
    error_message = "frontend_origin must be one canonical HTTPS origin without credentials, a port, or a path."
  }
}

variable "media_bucket_name" {
  description = "Existing private Cloud Storage bucket used for media objects."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.media_bucket_name))
    error_message = "media_bucket_name must be a 3 to 63 character lowercase Cloud Storage bucket name."
  }
}

variable "public_traffic_enabled" {
  description = "Open general edge traffic only after the deployment readiness gate succeeds."
  type        = bool
  default     = false
}

variable "stable_api_revision" {
  description = "Existing ready API revision that must retain all traffic until rollout succeeds."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.stable_api_revision == null ||
      can(regex("^[a-z][a-z0-9-]{0,62}$", var.stable_api_revision))
    )
    error_message = "stable_api_revision must be null or a valid Cloud Run revision name."
  }
}

variable "artifact_repository_id" {
  description = "Artifact Registry Docker repository used by the backend pipeline."
  type        = string
  default     = "backend"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.artifact_repository_id))
    error_message = "artifact_repository_id must be a lowercase repository identifier."
  }
}

variable "database_url_secret_id" {
  description = "Existing Secret Manager secret ID containing the SQLAlchemy database URL."
  type        = string
  default     = "seqret-database-url"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,255}$", var.database_url_secret_id))
    error_message = "database_url_secret_id must be a Secret Manager secret ID."
  }
}

variable "cloud_sql_instance_id" {
  description = "Existing same-project, same-region Cloud SQL PostgreSQL instance ID."
  type        = string

  validation {
    condition = (
      can(regex("^[a-z](?:[a-z0-9-]{0,96}[a-z0-9])?$", var.cloud_sql_instance_id)) &&
      length("/cloudsql/${var.project_id}:${var.region}:${var.cloud_sql_instance_id}/.s.PGSQL.5432") <= 107
    )
    error_message = "cloud_sql_instance_id must be valid and keep the Unix socket path at most 107 characters."
  }
}

variable "redis_url_secret_id" {
  description = "Optional existing Secret Manager secret ID containing the Redis URL."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.redis_url_secret_id == null ||
      can(regex("^[A-Za-z0-9_-]{1,255}$", var.redis_url_secret_id))
    )
    error_message = "redis_url_secret_id must be null or a Secret Manager secret ID."
  }
}

variable "redis_vpc_network" {
  description = "Optional existing VPC network used by the API for direct Redis egress."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      (
        var.redis_url_secret_id == null &&
        var.redis_vpc_network == null &&
        var.redis_vpc_subnetwork == null
      ) ||
      (
        var.redis_url_secret_id != null &&
        try(trimspace(var.redis_vpc_network) != "", false) &&
        try(trimspace(var.redis_vpc_subnetwork) != "", false)
      )
    )
    error_message = "redis_url_secret_id, redis_vpc_network, and redis_vpc_subnetwork must all be null or all be non-empty."
  }
}

variable "redis_vpc_subnetwork" {
  description = "Optional existing same-region subnet used by the API for direct Redis egress."
  type        = string
  default     = null
  nullable    = true
}

variable "monitoring_notification_channel_ids" {
  description = "Existing Cloud Monitoring notification channel resource names."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for channel in var.monitoring_notification_channel_ids :
      can(regex("^projects/[^/]+/notificationChannels/[0-9]+$", channel))
    ])
    error_message = "monitoring_notification_channel_ids must contain full notification channel resource names."
  }
}

variable "api_p95_latency_threshold_ms" {
  description = "API p95 request latency alert threshold in milliseconds."
  type        = number
  default     = 2000

  validation {
    condition     = var.api_p95_latency_threshold_ms > 0
    error_message = "api_p95_latency_threshold_ms must be positive."
  }
}

variable "otel_trace_sample_ratio" {
  description = "Local probability for application trace export."
  type        = number
  default     = 0.1

  validation {
    condition     = var.otel_trace_sample_ratio >= 0 && var.otel_trace_sample_ratio <= 1
    error_message = "otel_trace_sample_ratio must be between 0 and 1."
  }
}

variable "media_retention_days" {
  description = "Approved number of days completed-job media must be retained before deletion."
  type        = number

  validation {
    condition = (
      var.media_retention_days >= 1 &&
      var.media_retention_days <= 3650 &&
      floor(var.media_retention_days) == var.media_retention_days
    )
    error_message = "media_retention_days must be a whole number from 1 through 3650."
  }
}

variable "labels" {
  description = "Additional non-sensitive labels applied to Cloud Run resources."
  type        = map(string)
  default     = {}

  validation {
    condition = (
      length(setunion(
        toset(keys(var.labels)),
        toset(["application", "environment", "managed_by"]),
      )) <= 64 &&
      alltrue([
        for key, value in var.labels :
        can(regex("^[a-z](?:[a-z0-9_-]{0,61}[a-z0-9])?$", key)) &&
        (
          value == "" ||
          can(regex("^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$", value))
        )
      ])
    )
    error_message = "labels must contain at most 64 valid GCP label keys and lowercase values."
  }
}

variable "api_max_instances" {
  description = "Maximum number of API instances."
  type        = number
  default     = 10

  validation {
    condition     = var.api_max_instances >= 1
    error_message = "api_max_instances must be at least 1."
  }
}

variable "worker_max_instances" {
  description = "Maximum number of private worker instances."
  type        = number
  default     = 10

  validation {
    condition     = var.worker_max_instances >= 1
    error_message = "worker_max_instances must be at least 1."
  }
}

variable "api_command" {
  description = "Optional API container command override."
  type        = list(string)
  default     = []
}

variable "api_args" {
  description = "Optional API container argument override."
  type        = list(string)
  default     = []
}

variable "worker_runtime" {
  description = "B-owned worker image and entrypoint; null leaves the runtime unprovisioned."
  type = object({
    container_image = string
    command         = list(string)
    args            = optional(list(string), [])
  })
  default  = null
  nullable = true

  validation {
    condition = var.worker_runtime == null || try(
      length(var.worker_runtime.command) > 0 &&
      startswith(var.worker_runtime.container_image, "${var.region}-docker.pkg.dev/${var.project_id}/") &&
      can(regex("@sha256:[0-9a-f]{64}$", var.worker_runtime.container_image)),
      false,
    )
    error_message = "worker_runtime must use an explicit command and immutable same-project Artifact Registry digest."
  }
}

variable "job_runtime" {
  description = "B-owned media-job image and entrypoint; null leaves the runtime unprovisioned."
  type = object({
    container_image = string
    command         = list(string)
    args            = optional(list(string), [])
  })
  default  = null
  nullable = true

  validation {
    condition = var.job_runtime == null || try(
      length(var.job_runtime.command) > 0 &&
      startswith(var.job_runtime.container_image, "${var.region}-docker.pkg.dev/${var.project_id}/") &&
      can(regex("@sha256:[0-9a-f]{64}$", var.job_runtime.container_image)),
      false,
    )
    error_message = "job_runtime must use an explicit command and immutable same-project Artifact Registry digest."
  }
}

variable "job_max_retries" {
  description = "Maximum retries for one Cloud Run Job task."
  type        = number
  default     = 3

  validation {
    condition     = var.job_max_retries >= 0
    error_message = "job_max_retries cannot be negative."
  }
}

variable "job_timeout" {
  description = "Cloud Run Job task timeout as a duration ending in seconds."
  type        = string
  default     = "3600s"

  validation {
    condition     = can(regex("^[1-9][0-9]*s$", var.job_timeout))
    error_message = "job_timeout must be a positive duration in seconds such as 3600s."
  }
}

variable "migration_timeout" {
  description = "Deployment migration gate timeout as a duration ending in seconds."
  type        = string
  default     = "900s"

  validation {
    condition     = can(regex("^[1-9][0-9]*s$", var.migration_timeout))
    error_message = "migration_timeout must be a positive duration in seconds such as 900s."
  }
}
