resource "google_compute_region_network_endpoint_group" "api" {
  project               = var.project_id
  name                  = "${local.api_name}-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.api.name
  }

  depends_on = [google_project_service.required]
}

resource "google_compute_security_policy" "api" {
  project     = var.project_id
  name        = "${local.api_name}-armor"
  description = "Managed WAF policy for the public API edge"
  type        = "CLOUD_ARMOR"

  rule {
    action      = "throttle"
    priority    = 900
    description = "Limit health and readiness probes per client"
    match {
      expr {
        expression = "request.path == '/readyz' || request.path == '/healthz'"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 120
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 1000
    description = "Block SQL injection signatures"
    match {
      expr {
        expression = "evaluatePreconfiguredWaf('sqli-v33-stable', {'sensitivity': 1})"
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 1100
    description = "Block cross-site scripting signatures"
    match {
      expr {
        expression = "evaluatePreconfiguredWaf('xss-v33-stable', {'sensitivity': 1})"
      }
    }
  }

  rule {
    action   = var.public_traffic_enabled ? "allow" : "deny(502)"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_compute_backend_service" "api" {
  project               = var.project_id
  name                  = "${local.api_name}-backend"
  description           = "Cloud Run API behind the managed security edge"
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.api.id

  backend {
    group = google_compute_region_network_endpoint_group.api.id
  }

  log_config {
    enable      = true
    sample_rate = 1
  }

  depends_on = [google_project_service.required]
}

resource "google_compute_url_map" "api" {
  project         = var.project_id
  name            = "${local.api_name}-routes"
  default_service = google_compute_backend_service.api.id

  depends_on = [google_project_service.required]
}

resource "google_compute_managed_ssl_certificate" "api" {
  project = var.project_id
  name    = "${local.api_name}-cert-${substr(sha256(var.api_domain), 0, 8)}"

  managed {
    domains = [var.api_domain]
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [google_project_service.required]
}

resource "google_compute_target_https_proxy" "api" {
  project          = var.project_id
  name             = "${local.api_name}-https"
  url_map          = google_compute_url_map.api.id
  ssl_certificates = [google_compute_managed_ssl_certificate.api.id]

  depends_on = [google_project_service.required]
}

resource "google_compute_global_address" "api" {
  project = var.project_id
  name    = "${local.api_name}-address"

  depends_on = [google_project_service.required]
}

resource "google_compute_global_forwarding_rule" "api_https" {
  project               = var.project_id
  name                  = "${local.api_name}-https"
  target                = google_compute_target_https_proxy.api.id
  ip_address            = google_compute_global_address.api.address
  ip_protocol           = "TCP"
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"

  depends_on = [google_project_service.required]
}

resource "google_monitoring_uptime_check_config" "api" {
  project      = var.project_id
  display_name = "${local.api_name} HTTPS health"
  timeout      = "10s"
  period       = "60s"
  checker_type = "STATIC_IP_CHECKERS"

  selected_regions = [
    "ASIA_PACIFIC",
    "EUROPE",
    "USA",
  ]

  http_check {
    path           = "/edgez"
    port           = 443
    request_method = "GET"
    use_ssl        = true
    validate_ssl   = true

  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      host       = var.api_domain
      project_id = var.project_id
    }
  }

  user_labels = local.common_labels

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    google_project_service.observability,
  ]
}
