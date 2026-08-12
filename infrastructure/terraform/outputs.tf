output "api_service_name" {
  description = "Cloud Run API service name."
  value       = google_cloud_run_v2_service.api.name
}

output "api_service_uri" {
  description = "Cloud Run API service URI, reachable only through allowed ingress."
  value       = google_cloud_run_v2_service.api.uri
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
  value       = google_cloud_run_v2_job.job.name
}

output "runtime_service_accounts" {
  description = "Dedicated runtime service account emails by execution unit."
  value = {
    api    = google_service_account.api.email
    worker = google_service_account.worker.email
    job    = google_service_account.job.email
  }
}
