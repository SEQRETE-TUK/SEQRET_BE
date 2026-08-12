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
  description = "Immutable container image reference from Artifact Registry."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.container_image))
    error_message = "container_image must end with an immutable @sha256:<64 lowercase hex> digest."
  }
}

variable "deletion_protection" {
  description = "Protect Cloud Run resources from accidental Terraform deletion."
  type        = bool
  default     = true
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

variable "container_port" {
  description = "HTTP port exposed by API and worker containers."
  type        = number
  default     = 8080

  validation {
    condition     = var.container_port >= 1 && var.container_port <= 65535
    error_message = "container_port must be between 1 and 65535."
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

variable "worker_command" {
  description = "Optional private worker container command supplied by its owner."
  type        = list(string)
  default     = []
}

variable "worker_args" {
  description = "Optional private worker container arguments supplied by its owner."
  type        = list(string)
  default     = []
}

variable "job_command" {
  description = "Optional Cloud Run Job container command supplied by its owner."
  type        = list(string)
  default     = []
}

variable "job_args" {
  description = "Optional Cloud Run Job container arguments supplied by its owner."
  type        = list(string)
  default     = []
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
