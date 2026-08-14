data "google_project" "current" {
  project_id = var.project_id
}

locals {
  notification_subscription_name             = "${local.resource_stem}-notify"
  notification_dead_letter_topic_name        = "${local.resource_stem}-notify-dlq"
  notification_dead_letter_subscription_name = "${local.resource_stem}-notify-dlq-inspect"
  pubsub_service_agent                       = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic" "notification_dead_letter" {
  project                    = var.project_id
  name                       = local.notification_dead_letter_topic_name
  labels                     = local.common_labels
  message_retention_duration = "2678400s"
  deletion_policy            = var.deletion_protection ? "PREVENT" : "DELETE"

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic_iam_member" "pubsub_dead_letter_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.notification_dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_service_agent
}

resource "google_pubsub_subscription" "notification_events" {
  project = var.project_id
  name    = local.notification_subscription_name
  topic   = google_pubsub_topic.events.id
  labels = merge(local.common_labels, {
    replay_contract     = "v1"
    seqret_replay_state = "pending"
  })
  ack_deadline_seconds       = 300
  message_retention_duration = "2678400s"
  deletion_policy            = var.deletion_protection ? "PREVENT" : "DELETE"

  expiration_policy { ttl = "" }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.notification_dead_letter.id
    max_delivery_attempts = 5
  }

  lifecycle {
    ignore_changes = [labels["seqret_replay_state"]]
  }

  depends_on = [
    google_project_service.required,
    google_pubsub_topic_iam_member.pubsub_dead_letter_publisher,
  ]
}

resource "google_pubsub_subscription" "notification_dead_letter" {
  project                    = var.project_id
  name                       = local.notification_dead_letter_subscription_name
  topic                      = google_pubsub_topic.notification_dead_letter.id
  labels                     = local.common_labels
  ack_deadline_seconds       = 60
  message_retention_duration = "2678400s"
  deletion_policy            = var.deletion_protection ? "PREVENT" : "DELETE"

  expiration_policy { ttl = "" }
}

resource "google_pubsub_subscription_iam_member" "pubsub_dead_letter_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.notification_events.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_service_agent
}

resource "google_pubsub_subscription_iam_member" "outbox_relay_notification_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.notification_events.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.outbox_relay.email}"
}

resource "google_pubsub_subscription_iam_member" "outbox_relay_notification_viewer" {
  project      = var.project_id
  subscription = google_pubsub_subscription.notification_events.name
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${google_service_account.outbox_relay.email}"
}

resource "google_monitoring_alert_policy" "notification_dead_letter_backlog" {
  project               = var.project_id
  display_name          = "${local.notification_dead_letter_subscription_name} backlog"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "ERROR"
  user_labels           = local.common_labels

  conditions {
    display_name = "At least one notification event is waiting in the DLQ"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"pubsub_subscription\"",
        "resource.label.subscription_id=\"${local.notification_dead_letter_subscription_name}\"",
        "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  alert_strategy { auto_close = "1800s" }
  documentation {
    subject   = "Notification events require DLQ inspection"
    content   = "Inspect without acknowledging, repair the underlying data or contract, and replay through the source topic only after documenting the event IDs."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}

resource "google_monitoring_alert_policy" "notification_source_backlog_age" {
  project               = var.project_id
  display_name          = "${local.notification_subscription_name} oldest unacked event"
  combiner              = "OR"
  notification_channels = var.monitoring_notification_channel_ids
  severity              = "WARNING"
  user_labels           = local.common_labels

  conditions {
    display_name = "The oldest notification event has remained unacked for fifteen minutes"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"pubsub_subscription\"",
        "resource.label.subscription_id=\"${local.notification_subscription_name}\"",
        "metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 900
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }
  documentation {
    subject   = "${local.notification_subscription_name} has a stale event backlog"
    content   = "The oldest unacked source event exceeded the native retry window. Inspect relay executions, subscription initialization, and the DLQ before the 31-day retention limit."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.observability]
}
