mock_provider "google" {}

variables {
  project_id                          = "seqret-staging"
  region                              = "asia-northeast3"
  environment                         = "staging"
  api_domain                          = "api.staging.example.com"
  frontend_origin                     = "https://staging.example.com"
  media_bucket_name                   = "seqret-stg-media"
  container_image                     = "asia-northeast3-docker.pkg.dev/seqret-staging/backend/app@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  cloud_sql_instance_id               = "seqret-stg-db"
  migration_database_url_secret_id    = "seqret-migration-database-url"
  api_database_url_secret_id          = "seqret-api-database-url"
  worker_database_url_secret_id       = "seqret-worker-database-url"
  outbox_relay_database_url_secret_id = "seqret-relay-database-url"
  media_retention_days                = 30
}

run "external_delivery_is_disabled_by_default" {
  command = apply

  assert {
    condition = alltrue([
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_NOTIFICATION_DELIVERY_ENABLED"
      ]) == "false",
      length([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env
        if startswith(env.name, "SEQRET_NHN_NOTIFICATION_")
      ]) == 0,
      length(google_secret_manager_secret_iam_member.outbox_relay_notification) == 0,
    ])
    error_message = "External delivery must remain off and receive no NHN credentials by default."
  }
}

run "external_delivery_receives_complete_nhn_configuration" {
  command = apply

  variables {
    notification_delivery_enabled               = true
    nhn_notification_email_app_key              = "email-app"
    nhn_notification_email_secret_key_secret_id = "seqret-nhn-email-secret"
    nhn_notification_email_sender_address       = "notice@seqret.example.com"
    nhn_notification_email_sender_name          = "SEQRET"
    nhn_notification_sms_app_key                = "sms-app"
    nhn_notification_sms_secret_key_secret_id   = "seqret-nhn-sms-secret"
    nhn_notification_sms_sender_number          = "0212345678"
    nhn_notification_kakao_app_key              = "kakao-app"
    nhn_notification_kakao_secret_key_secret_id = "seqret-nhn-kakao-secret"
    nhn_notification_kakao_sender_key           = "0123456789012345678901234567890123456789"
    nhn_notification_kakao_template_code        = "SEQRET_NOTICE"
  }

  assert {
    condition = alltrue([
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_NOTIFICATION_DELIVERY_ENABLED"
      ]) == "true",
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_NHN_NOTIFICATION_EMAIL_APP_KEY"
      ]) == "email-app",
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_NHN_NOTIFICATION_SMS_SENDER_NUMBER"
      ]) == "0212345678",
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value
        if env.name == "SEQRET_NHN_NOTIFICATION_KAKAO_TEMPLATE_CODE"
      ]) == "SEQRET_NOTICE",
      length(google_secret_manager_secret_iam_member.outbox_relay_notification) == 3,
      alltrue([
        for grant in google_secret_manager_secret_iam_member.outbox_relay_notification :
        grant.role == "roles/secretmanager.secretAccessor" &&
        grant.member == "serviceAccount:${google_service_account.outbox_relay.email}"
      ]),
    ])
    error_message = "Enabled external delivery must receive complete non-secret settings and exactly three secret grants."
  }

  assert {
    condition = alltrue([
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].secret
        if env.name == "SEQRET_NHN_NOTIFICATION_EMAIL_SECRET_KEY"
      ]) == "seqret-nhn-email-secret",
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].secret
        if env.name == "SEQRET_NHN_NOTIFICATION_SMS_SECRET_KEY"
      ]) == "seqret-nhn-sms-secret",
      one([
        for env in google_cloud_run_v2_job.outbox_relay.template[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].secret
        if env.name == "SEQRET_NHN_NOTIFICATION_KAKAO_SECRET_KEY"
      ]) == "seqret-nhn-kakao-secret",
    ])
    error_message = "NHN secret values must come only from their configured Secret Manager IDs."
  }
}

run "enabled_external_delivery_requires_every_nhn_setting" {
  command = plan

  variables {
    notification_delivery_enabled               = true
    nhn_notification_email_app_key              = "email-app"
    nhn_notification_email_secret_key_secret_id = "seqret-nhn-email-secret"
    nhn_notification_email_sender_address       = "notice@seqret.example.com"
    nhn_notification_email_sender_name          = "SEQRET"
    nhn_notification_sms_app_key                = "sms-app"
    nhn_notification_sms_secret_key_secret_id   = "seqret-nhn-sms-secret"
    nhn_notification_sms_sender_number          = "0212345678"
    nhn_notification_kakao_app_key              = "kakao-app"
    nhn_notification_kakao_secret_key_secret_id = "seqret-nhn-kakao-secret"
    nhn_notification_kakao_sender_key           = "0123456789012345678901234567890123456789"
  }

  expect_failures = [check.notification_delivery_configuration]
}

run "invalid_nhn_secret_id_is_rejected" {
  command = plan

  variables {
    nhn_notification_email_secret_key_secret_id = "projects/example/secrets/raw"
  }

  expect_failures = [var.nhn_notification_email_secret_key_secret_id]
}

run "invalid_nhn_sms_sender_is_rejected" {
  command = plan

  variables {
    nhn_notification_sms_sender_number = "02-1234-5678"
  }

  expect_failures = [var.nhn_notification_sms_sender_number]
}

run "oversized_nhn_email_sender_is_rejected" {
  command = plan

  variables {
    nhn_notification_email_sender_address = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@example.com"
  }

  expect_failures = [var.nhn_notification_email_sender_address]
}

run "invalid_kakao_sender_key_is_rejected" {
  command = plan

  variables {
    nhn_notification_kakao_sender_key = "short"
  }

  expect_failures = [var.nhn_notification_kakao_sender_key]
}

run "notification_secret_ids_must_be_distinct" {
  command = plan

  variables {
    notification_delivery_enabled               = true
    nhn_notification_email_app_key              = "email-app"
    nhn_notification_email_secret_key_secret_id = "seqret-api-database-url"
    nhn_notification_email_sender_address       = "notice@seqret.example.com"
    nhn_notification_email_sender_name          = "SEQRET"
    nhn_notification_sms_app_key                = "sms-app"
    nhn_notification_sms_secret_key_secret_id   = "seqret-nhn-sms-secret"
    nhn_notification_sms_sender_number          = "0212345678"
    nhn_notification_kakao_app_key              = "kakao-app"
    nhn_notification_kakao_secret_key_secret_id = "seqret-nhn-kakao-secret"
    nhn_notification_kakao_sender_key           = "0123456789012345678901234567890123456789"
    nhn_notification_kakao_template_code        = "SEQRET_NOTICE"
  }

  expect_failures = [check.notification_secret_ids_are_distinct]
}
