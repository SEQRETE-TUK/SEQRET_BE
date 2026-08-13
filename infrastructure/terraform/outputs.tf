output "api_service_name" {
  description = "Cloud Run API service name."
  value       = google_cloud_run_v2_service.api.name
}

output "worker_service_name" {
  description = "Private Cloud Run worker service name."
  value       = google_cloud_run_v2_service.worker.name
}

output "worker_service_uri" {
  description = "Private Cloud Run worker URI."
  value       = google_cloud_run_v2_service.worker.uri
}

output "job_name" {
  description = "Cloud Run Job name."
  value       = one(google_cloud_run_v2_job.job[*].name)
}

output "api_public_endpoint" {
  description = "Public HTTPS API endpoint protected by Cloud Armor."
  value       = "https://${var.api_domain}"
}

output "api_load_balancer_ip" {
  description = "Global address that the API DNS record must resolve to."
  value       = google_compute_global_address.api.address
}

output "api_managed_certificate_name" {
  description = "Managed certificate name for the configured API domain."
  value       = google_compute_managed_ssl_certificate.api.name
}

output "artifact_repository" {
  description = "Artifact Registry repository resource name."
  value       = google_artifact_registry_repository.backend.name
}

output "migration_job_name" {
  description = "Cloud Run migration gate Job name."
  value       = google_cloud_run_v2_job.migration.name
}

output "observability_dashboard_id" {
  description = "Cloud Monitoring runtime dashboard resource ID."
  value       = google_monitoring_dashboard.runtime.id
}

output "runtime_service_accounts" {
  description = "Dedicated runtime service account emails by execution unit."
  value = {
    api       = google_service_account.api.email
    worker    = google_service_account.worker.email
    job       = google_service_account.job.email
    migration = google_service_account.migration.email
    relay     = google_service_account.outbox_relay.email
    task      = google_service_account.task_invoker.email
  }
}
