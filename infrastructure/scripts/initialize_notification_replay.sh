#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 3 ]]; then
  echo "usage: $0 PROJECT_ID SUBSCRIPTION_ID TOPIC_ID" >&2
  exit 2
fi

project_id="$1"
subscription_id="$2"
topic_id="$3"
expected_topic="projects/${project_id}/topics/${topic_id}"

if [[ ! "${project_id}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] \
  || [[ ! "${subscription_id}" =~ ^[A-Za-z][A-Za-z0-9._~+%-]{2,254}$ ]] \
  || [[ ! "${topic_id}" =~ ^[A-Za-z][A-Za-z0-9._~+%-]{2,254}$ ]]; then
  echo "project, subscription, or topic identifier is invalid" >&2
  exit 2
fi

describe_subscription() {
  gcloud pubsub subscriptions describe "${subscription_id}" \
    --project "${project_id}" \
    --format=json
}

subscription_json="$(describe_subscription)"
actual_topic="$(jq -r '.topic // ""' <<<"${subscription_json}")"
replay_contract="$(jq -r '.labels.replay_contract // ""' <<<"${subscription_json}")"
replay_state="$(jq -r '.labels.seqret_replay_state // ""' <<<"${subscription_json}")"

if [[ "${actual_topic}" != "${expected_topic}" || "${replay_contract}" != "v1" ]]; then
  echo "subscription does not match the Terraform-managed replay contract" >&2
  exit 1
fi
if [[ "${replay_state}" != "pending" ]]; then
  echo "replay initialization is already claimed or complete: ${replay_state}" >&2
  exit 1
fi

gcloud pubsub subscriptions update "${subscription_id}" \
  --project "${project_id}" \
  --update-labels=seqret_replay_state=initializing \
  --quiet

claimed_json="$(describe_subscription)"
claimed_state="$(jq -r '.labels.seqret_replay_state // ""' <<<"${claimed_json}")"
claimed_topic="$(jq -r '.topic // ""' <<<"${claimed_json}")"
claimed_contract="$(jq -r '.labels.replay_contract // ""' <<<"${claimed_json}")"
if [[ "${claimed_state}" != "initializing" \
  || "${claimed_topic}" != "${expected_topic}" \
  || "${claimed_contract}" != "v1" ]]; then
  echo "failed to claim replay initialization" >&2
  exit 1
fi

replay_start="$(date -u -d '31 days ago' '+%Y-%m-%dT%H:%M:%SZ')"
gcloud pubsub subscriptions seek "${subscription_id}" \
  --project "${project_id}" \
  --time "${replay_start}" \
  --quiet

gcloud pubsub subscriptions update "${subscription_id}" \
  --project "${project_id}" \
  --update-labels=seqret_replay_state=complete \
  --quiet

completed_json="$(describe_subscription)"
completed_state="$(jq -r '.labels.seqret_replay_state // ""' <<<"${completed_json}")"
completed_topic="$(jq -r '.topic // ""' <<<"${completed_json}")"
completed_contract="$(jq -r '.labels.replay_contract // ""' <<<"${completed_json}")"
if [[ "${completed_state}" != "complete" \
  || "${completed_topic}" != "${expected_topic}" \
  || "${completed_contract}" != "v1" ]]; then
  echo "seek succeeded but the completion label was not persisted" >&2
  exit 1
fi

echo "Notification replay initialized from ${replay_start}."
