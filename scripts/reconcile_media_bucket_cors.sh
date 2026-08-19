#!/usr/bin/env bash

set -euo pipefail

if (( $# != 3 )); then
  echo "Usage: $0 PROJECT_ID BUCKET_NAME FRONTEND_ORIGIN" >&2
  exit 2
fi

project_id="$1"
bucket_name="$2"
frontend_origin="$3"
verify_attempts="${SEQRET_CORS_VERIFY_ATTEMPTS:-5}"
verify_delay_seconds="${SEQRET_CORS_VERIFY_DELAY_SECONDS:-2}"

if [[ ! "${project_id}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  echo "PROJECT_ID is invalid." >&2
  exit 2
fi
if [[ ! "${bucket_name}" =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ ]]; then
  echo "BUCKET_NAME must be a 3 to 63 character lowercase Cloud Storage bucket name." >&2
  exit 2
fi
if [[ ! "${frontend_origin}" =~ ^https://[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
  echo "FRONTEND_ORIGIN must be one canonical HTTPS origin without a port or path." >&2
  exit 2
fi
if [[ ! "${verify_attempts}" =~ ^[1-9][0-9]*$ ]] || (( verify_attempts > 30 )); then
  echo "SEQRET_CORS_VERIFY_ATTEMPTS must be a whole number from 1 through 30." >&2
  exit 2
fi
if [[ ! "${verify_delay_seconds}" =~ ^[0-9]+$ ]] || (( verify_delay_seconds > 30 )); then
  echo "SEQRET_CORS_VERIFY_DELAY_SECONDS must be a whole number from 0 through 30." >&2
  exit 2
fi

for command_name in gcloud jq; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "${command_name} is required to reconcile media bucket CORS." >&2
    exit 2
  fi
done

cors_file="$(mktemp "${TMPDIR:-/tmp}/seqret-media-cors.XXXXXX.json")"
trap 'rm -f -- "${cors_file}"' EXIT

jq -n --arg origin "${frontend_origin}" '
  [{
    origin: [$origin],
    method: ["GET", "HEAD", "PUT"],
    responseHeader: [
      "Content-Type",
      "x-goog-if-generation-match",
      "x-goog-generation",
      "x-goog-hash"
    ],
    maxAgeSeconds: 3600
  }]
' > "${cors_file}"

gcloud storage buckets update "gs://${bucket_name}" \
  --project "${project_id}" \
  --cors-file "${cors_file}" \
  --quiet \
  >/dev/null

actual_bucket=""
for (( attempt = 1; attempt <= verify_attempts; attempt++ )); do
  actual_bucket="$(gcloud storage buckets describe "gs://${bucket_name}" \
    --project "${project_id}" \
    --format=json)"
  if jq -e --arg origin "${frontend_origin}" '
    (.cors_config // []) as $cors
    | ($cors | length) == 1
      and ($cors[0].origin == [$origin])
      and (($cors[0].method | sort) == ["GET", "HEAD", "PUT"])
      and (($cors[0].responseHeader | sort) == (
        [
          "Content-Type",
          "x-goog-if-generation-match",
          "x-goog-generation",
          "x-goog-hash"
        ] | sort
      ))
      and ($cors[0].maxAgeSeconds == 3600)
  ' <<< "${actual_bucket}" >/dev/null; then
    echo "Media bucket CORS matches ${frontend_origin}."
    exit 0
  fi
  if (( attempt < verify_attempts )); then
    sleep "${verify_delay_seconds}"
  fi
done

echo "Media bucket CORS did not converge to the canonical frontend origin." >&2
jq -c '.cors_config // null' <<< "${actual_bucket}" >&2
exit 1
