mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789"
    }
  }
}

variables {
  project_id            = "seqret-staging"
  region                = "asia-northeast3"
  environment           = "staging"
  api_domain            = "api.staging.example.com"
  frontend_origin       = "https://staging.example.com"
  container_image       = "asia-northeast3-docker.pkg.dev/seqret-staging/backend/app@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  cloud_sql_instance_id = "seqret-stg-db"
  media_retention_days  = 30
}

run "notification_event_pump" {
  command = apply

  assert {
    condition = alltrue([
      google_pubsub_subscription.notification_events.topic == google_pubsub_topic.events.id,
      google_pubsub_subscription.notification_events.ack_deadline_seconds == 300,
      google_pubsub_subscription.notification_events.message_retention_duration == "2678400s",
      google_pubsub_subscription.notification_events.deletion_policy == "PREVENT",
      google_pubsub_subscription.notification_events.expiration_policy[0].ttl == "",
      google_pubsub_subscription.notification_events.retry_policy[0].minimum_backoff == "10s",
      google_pubsub_subscription.notification_events.retry_policy[0].maximum_backoff == "600s",
      google_pubsub_subscription.notification_events.dead_letter_policy[0].max_delivery_attempts == 5,
      google_pubsub_subscription.notification_events.dead_letter_policy[0].dead_letter_topic == google_pubsub_topic.notification_dead_letter.id,
      google_pubsub_subscription.notification_events.labels.replay_contract == "v1",
      google_pubsub_subscription.notification_events.labels.seqret_replay_state == "pending",
    ])
    error_message = "The source subscription must retain events and use guarded replay plus native DLQ retry."
  }

  assert {
    condition = alltrue([
      google_pubsub_topic.notification_dead_letter.message_retention_duration == "2678400s",
      google_pubsub_topic.notification_dead_letter.deletion_policy == "PREVENT",
      google_pubsub_subscription.notification_dead_letter.topic == google_pubsub_topic.notification_dead_letter.id,
      google_pubsub_subscription.notification_dead_letter.message_retention_duration == "2678400s",
      google_pubsub_subscription.notification_dead_letter.deletion_policy == "PREVENT",
      google_pubsub_subscription.notification_dead_letter.expiration_policy[0].ttl == "",
    ])
    error_message = "The DLQ must remain inspectable for the full retained-event window."
  }

  assert {
    condition = alltrue([
      google_pubsub_subscription_iam_member.outbox_relay_notification_subscriber.role == "roles/pubsub.subscriber",
      google_pubsub_subscription_iam_member.outbox_relay_notification_subscriber.subscription == google_pubsub_subscription.notification_events.name,
      google_pubsub_subscription_iam_member.outbox_relay_notification_subscriber.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_pubsub_subscription_iam_member.outbox_relay_notification_viewer.role == "roles/pubsub.viewer",
      google_pubsub_subscription_iam_member.outbox_relay_notification_viewer.subscription == google_pubsub_subscription.notification_events.name,
      google_pubsub_subscription_iam_member.outbox_relay_notification_viewer.member == "serviceAccount:${google_service_account.outbox_relay.email}",
      google_pubsub_topic_iam_member.pubsub_dead_letter_publisher.role == "roles/pubsub.publisher",
      google_pubsub_topic_iam_member.pubsub_dead_letter_publisher.member == "serviceAccount:service-123456789@gcp-sa-pubsub.iam.gserviceaccount.com",
      google_pubsub_subscription_iam_member.pubsub_dead_letter_subscriber.role == "roles/pubsub.subscriber",
      google_pubsub_subscription_iam_member.pubsub_dead_letter_subscriber.member == "serviceAccount:service-123456789@gcp-sa-pubsub.iam.gserviceaccount.com",
    ])
    error_message = "The relay and Pub/Sub forwarding service agent must keep least-privilege subscription IAM."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_job.outbox_relay.template[0].template[0].timeout == "240s",
      join(" ", google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].command) == "python -m app.entrypoints.outbox_relay",
      google_cloud_run_v2_job.outbox_relay.template[0].template[0].service_account == google_service_account.outbox_relay.email,
      one([for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value if env.name == "SEQRET_PUBSUB_SUBSCRIPTION_ID"]) == "seqret-stg-notify",
      one([for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value if env.name == "SEQRET_PUBSUB_TOPIC_ID"]) == "seqret-stg-events",
    ])
    error_message = "The existing relay Job must run the bounded notification event pump."
  }

  assert {
    condition = alltrue([
      strcontains(google_monitoring_alert_policy.notification_dead_letter_backlog.conditions[0].condition_threshold[0].filter, "seqret-stg-notify-dlq-inspect"),
      google_monitoring_alert_policy.notification_source_backlog_age.conditions[0].condition_threshold[0].threshold_value == 900,
      google_monitoring_alert_policy.notification_source_backlog_age.conditions[0].condition_threshold[0].duration == "300s",
      strcontains(google_monitoring_alert_policy.notification_source_backlog_age.conditions[0].condition_threshold[0].filter, "pubsub.googleapis.com/subscription/oldest_unacked_message_age"),
      google_monitoring_alert_policy.notification_source_backlog_age.alert_strategy[0].notification_rate_limit[0].period == "900s",
      length(google_monitoring_alert_policy.outbox_relay_failures.conditions) == 2,
    ])
    error_message = "Relay failures, source backlog age, and DLQ backlog must be observable."
  }
}
