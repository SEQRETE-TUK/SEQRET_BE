from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _fake_gcloud(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "gcloud-commands.jsonl"
    state_path = tmp_path / "bucket.json"
    executable = bin_dir / "gcloud"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
log_path = pathlib.Path(os.environ["FAKE_GCLOUD_LOG"])
with log_path.open("a", encoding="utf-8") as log_file:
    log_file.write(json.dumps(args) + "\\n")

if args[:3] == ["storage", "buckets", "update"]:
    cors_index = args.index("--cors-file")
    cors_path = pathlib.Path(args[cors_index + 1])
    state_path = pathlib.Path(os.environ["FAKE_GCLOUD_STATE"])
    state_path.write_text(
        json.dumps({"cors_config": json.loads(cors_path.read_text(encoding="utf-8"))}),
        encoding="utf-8",
    )
    raise SystemExit(0)

if args[:3] == ["storage", "buckets", "describe"]:
    override = os.environ.get("FAKE_GCLOUD_DESCRIBE_OVERRIDE")
    if override:
        print(override)
    else:
        print(pathlib.Path(os.environ["FAKE_GCLOUD_STATE"]).read_text(encoding="utf-8"))
    raise SystemExit(0)

raise SystemExit(64)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_dir, command_log, state_path


def _run_script(
    tmp_path: Path,
    *,
    describe_override: dict[str, object] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bin_dir, command_log, state_path = _fake_gcloud(tmp_path)
    script = Path(__file__).parents[1] / "scripts" / "reconcile_media_bucket_cors.sh"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_GCLOUD_LOG": str(command_log),
            "FAKE_GCLOUD_STATE": str(state_path),
            "SEQRET_CORS_VERIFY_ATTEMPTS": "1",
            "SEQRET_CORS_VERIFY_DELAY_SECONDS": "0",
        }
    )
    if describe_override is not None:
        env["FAKE_GCLOUD_DESCRIBE_OVERRIDE"] = json.dumps(describe_override)
    result = subprocess.run(
        [
            "bash",
            str(script),
            "seqret-stg-20260813",
            "seqret-stg-20260813-media",
            "https://seqret.vercel.app",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    return result, command_log, state_path


def test_reconcile_media_bucket_cors_applies_exact_frontend_rule(tmp_path: Path) -> None:
    result, command_log, state_path = _run_script(tmp_path)

    assert result.returncode == 0
    assert "matches https://seqret.vercel.app" in result.stdout
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "cors_config": [
            {
                "origin": ["https://seqret.vercel.app"],
                "method": ["GET", "HEAD", "PUT"],
                "responseHeader": [
                    "Content-Type",
                    "x-goog-if-generation-match",
                    "x-goog-generation",
                    "x-goog-hash",
                ],
                "maxAgeSeconds": 3600,
            }
        ]
    }
    commands = [json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()]
    assert commands[0][:4] == [
        "storage",
        "buckets",
        "update",
        "gs://seqret-stg-20260813-media",
    ]
    assert "--project" in commands[0]
    assert "seqret-stg-20260813" in commands[0]
    assert commands[1][:4] == [
        "storage",
        "buckets",
        "describe",
        "gs://seqret-stg-20260813-media",
    ]


def test_reconcile_media_bucket_cors_fails_closed_on_extra_origin(tmp_path: Path) -> None:
    result, _, _ = _run_script(
        tmp_path,
        describe_override={
            "cors_config": [
                {
                    "origin": [
                        "https://seqret.vercel.app",
                        "https://unexpected.example.com",
                    ],
                    "method": ["GET", "HEAD", "PUT"],
                    "responseHeader": [
                        "Content-Type",
                        "x-goog-if-generation-match",
                        "x-goog-generation",
                        "x-goog-hash",
                    ],
                    "maxAgeSeconds": 3600,
                }
            ]
        },
    )

    assert result.returncode == 1
    assert "did not converge" in result.stderr
