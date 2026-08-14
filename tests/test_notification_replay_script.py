"""Guarded one-time Pub/Sub replay script tests."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infrastructure" / "scripts" / "initialize_notification_replay.sh"


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None,
    reason="requires the Linux CI shell toolchain",
)
@pytest.mark.parametrize("seek_fails", [False, True])
def test_replay_script_completes_only_after_successful_seek(
    tmp_path: Path,
    seek_fails: bool,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_file = tmp_path / "state"
    state_file.write_text("pending", encoding="utf-8")
    call_log = tmp_path / "gcloud.log"
    fake_gcloud = bin_dir / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GCLOUD_LOG}"
case "$1 $2 $3" in
  "pubsub subscriptions describe")
    printf '{"topic":"%s","labels":{"replay_contract":"v1","seqret_replay_state":"%s"}}\n' \
      "${FAKE_EXPECTED_TOPIC}" "$(<"${FAKE_STATE_FILE}")"
    ;;
  "pubsub subscriptions update")
    if [[ "$*" == *"seqret_replay_state=initializing"* ]]; then
      printf 'initializing' > "${FAKE_STATE_FILE}"
    elif [[ "$*" == *"seqret_replay_state=complete"* ]]; then
      printf 'complete' > "${FAKE_STATE_FILE}"
    else
      exit 2
    fi
    ;;
  "pubsub subscriptions seek")
    [[ "$(<"${FAKE_STATE_FILE}")" == "initializing" ]]
    [[ "${FAKE_SEEK_FAIL}" != "1" ]]
    ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    fake_jq = bin_dir / "jq"
    fake_jq.write_text(
        """#!/usr/bin/env python3
import json
import sys

document = json.load(sys.stdin)
query = sys.argv[-1]
if query.startswith(".topic"):
    value = document.get("topic", "")
elif "replay_contract" in query:
    value = document.get("labels", {}).get("replay_contract", "")
elif "seqret_replay_state" in query:
    value = document.get("labels", {}).get("seqret_replay_state", "")
else:
    raise SystemExit(2)
print(value)
""",
        encoding="utf-8",
    )
    fake_jq.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GCLOUD_LOG": str(call_log),
        "FAKE_STATE_FILE": str(state_file),
        "FAKE_EXPECTED_TOPIC": "projects/seqret-test/topics/domain-events",
        "FAKE_SEEK_FAIL": "1" if seek_fails else "0",
    }

    result = subprocess.run(
        [
            shutil.which("bash") or "bash",
            str(SCRIPT),
            "seqret-test",
            "participant-notifications",
            "domain-events",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert "seqret_replay_state=initializing" in calls[1]
    assert calls[3].startswith("pubsub subscriptions seek ")
    if seek_fails:
        assert result.returncode != 0
        assert state_file.read_text(encoding="utf-8") == "initializing"
        assert not any("seqret_replay_state=complete" in call for call in calls)
    else:
        assert result.returncode == 0
        assert state_file.read_text(encoding="utf-8") == "complete"
        assert "seqret_replay_state=complete" in calls[4]


def test_replay_script_enforces_pending_seek_complete_order() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    pending = script.index('"${replay_state}" != "pending"')
    initializing = script.index("seqret_replay_state=initializing")
    seek = script.index("gcloud pubsub subscriptions seek")
    complete = script.index("seqret_replay_state=complete")

    assert pending < initializing < seek < complete
