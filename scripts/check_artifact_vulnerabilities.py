#!/usr/bin/env python3
"""Enforce the staging Artifact Analysis vulnerability policy."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

BLOCKING_SEVERITIES = ("CRITICAL", "HIGH")


@dataclass(frozen=True, order=True)
class FindingKey:
    severity: str
    vulnerability_id: str
    package: str


@dataclass(frozen=True)
class Finding:
    key: FindingKey
    installed_version: str
    fixed_version: str | None


@dataclass(frozen=True)
class ExceptionEntry:
    key: FindingKey
    expires_on: date
    reason: str
    tracking_issue: str


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read valid JSON from {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _version_name(value: object) -> str:
    if not isinstance(value, dict):
        return "unknown"
    for field in ("fullName", "name", "revision", "kind"):
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate:
            return candidate
    return "unknown"


def _scan_state(payload: dict[str, Any]) -> tuple[str, str]:
    summary = payload.get("discovery_summary")
    discoveries = summary.get("discovery") if isinstance(summary, dict) else None
    if not isinstance(discoveries, list):
        return "MISSING", "UNKNOWN"
    for occurrence in discoveries:
        if not isinstance(occurrence, dict):
            continue
        note_name = occurrence.get("noteName")
        if not isinstance(note_name, str) or not note_name.endswith("/PACKAGE_VULNERABILITY"):
            continue
        discovery = occurrence.get("discovery")
        if not isinstance(discovery, dict):
            continue
        status = discovery.get("analysisStatus")
        continuous = discovery.get("continuousAnalysis")
        return (
            status if isinstance(status, str) else "MISSING",
            continuous if isinstance(continuous, str) else "UNKNOWN",
        )
    return "MISSING", "UNKNOWN"


def _findings(payload: dict[str, Any]) -> tuple[dict[FindingKey, Finding], list[str]]:
    errors: list[str] = []
    package_summary = payload.get("package_vulnerability_summary")
    vulnerabilities = (
        package_summary.get("vulnerabilities") if isinstance(package_summary, dict) else None
    )
    if not isinstance(vulnerabilities, dict):
        return {}, ["scan result has no package vulnerability summary"]

    findings: dict[FindingKey, Finding] = {}
    for severity in BLOCKING_SEVERITIES:
        occurrences = vulnerabilities.get(severity, [])
        if not isinstance(occurrences, list):
            errors.append(f"{severity} vulnerability collection must be an array")
            continue
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                errors.append(f"{severity} vulnerability occurrence must be an object")
                continue
            note_name = occurrence.get("noteName")
            vulnerability_id = (
                note_name.rsplit("/", maxsplit=1)[-1]
                if isinstance(note_name, str) and note_name
                else ""
            )
            vulnerability = occurrence.get("vulnerability")
            package_issues = (
                vulnerability.get("packageIssue") if isinstance(vulnerability, dict) else None
            )
            if not vulnerability_id or not isinstance(package_issues, list) or not package_issues:
                errors.append(f"{severity} occurrence lacks an ID or affected package")
                continue
            for package_issue in package_issues:
                if not isinstance(package_issue, dict):
                    errors.append(f"{vulnerability_id} package issue must be an object")
                    continue
                package = package_issue.get("affectedPackage")
                if not isinstance(package, str) or not package:
                    errors.append(f"{vulnerability_id} package issue lacks affectedPackage")
                    continue
                fixed = package_issue.get("fixedVersion")
                fixed_kind = fixed.get("kind") if isinstance(fixed, dict) else None
                fixed_version = None if fixed_kind == "MAXIMUM" else _version_name(fixed)
                key = FindingKey(severity, vulnerability_id, package)
                finding = Finding(
                    key=key,
                    installed_version=_version_name(package_issue.get("affectedVersion")),
                    fixed_version=fixed_version,
                )
                previous = findings.get(key)
                if previous is None or (
                    previous.fixed_version is None and finding.fixed_version is not None
                ):
                    findings[key] = finding
    return findings, errors


def _exceptions(payload: dict[str, Any]) -> tuple[dict[FindingKey, ExceptionEntry], list[str]]:
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("exception file schema_version must be 1")
    raw_entries = payload.get("exceptions")
    if not isinstance(raw_entries, list):
        return {}, [*errors, "exception file exceptions must be an array"]

    entries: dict[FindingKey, ExceptionEntry] = {}
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            errors.append(f"exception {index} must be an object")
            continue
        severity = raw_entry.get("severity")
        vulnerability_id = raw_entry.get("vulnerability_id")
        package = raw_entry.get("package")
        expires_raw = raw_entry.get("expires_on")
        reason = raw_entry.get("reason")
        tracking_issue = raw_entry.get("tracking_issue")
        if (
            severity not in BLOCKING_SEVERITIES
            or not isinstance(vulnerability_id, str)
            or not vulnerability_id
            or not isinstance(package, str)
            or not package
            or not isinstance(expires_raw, str)
            or not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(tracking_issue, str)
            or not tracking_issue.startswith("https://github.com/SEQRETE-TUK/SEQRET_BE/issues/")
        ):
            errors.append(f"exception {index} has invalid required fields")
            continue
        try:
            expires_on = date.fromisoformat(expires_raw)
        except ValueError:
            errors.append(f"exception {index} expires_on must be an ISO date")
            continue
        key = FindingKey(severity, vulnerability_id, package)
        if key in entries:
            errors.append(f"duplicate exception for {severity} {vulnerability_id} {package}")
            continue
        entries[key] = ExceptionEntry(
            key=key,
            expires_on=expires_on,
            reason=reason.strip(),
            tracking_issue=tracking_issue,
        )
    return entries, errors


def evaluate(
    scan: dict[str, Any],
    exception_payload: dict[str, Any],
    *,
    today: date,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    status, continuous = _scan_state(scan)
    if status != "FINISHED_SUCCESS":
        errors.append(f"package vulnerability scan status is {status}, not FINISHED_SUCCESS")
    if continuous != "ACTIVE":
        errors.append(f"continuous vulnerability analysis is {continuous}, not ACTIVE")

    findings, finding_errors = _findings(scan)
    exceptions, exception_errors = _exceptions(exception_payload)
    errors.extend(finding_errors)
    errors.extend(exception_errors)

    fixable = {key: finding for key, finding in findings.items() if finding.fixed_version}
    no_fix = {key: finding for key, finding in findings.items() if not finding.fixed_version}
    covered_no_fix: set[FindingKey] = set()
    for finding in sorted(fixable.values(), key=lambda item: item.key):
        errors.append(
            f"fix available for {finding.key.severity} {finding.key.vulnerability_id} "
            f"{finding.key.package}: {finding.fixed_version}"
        )
    for key in sorted(no_fix):
        exception = exceptions.get(key)
        if exception is None:
            errors.append(
                f"missing exception for {key.severity} {key.vulnerability_id} {key.package}"
            )
        elif exception.expires_on < today:
            errors.append(
                f"expired exception for {key.severity} {key.vulnerability_id} "
                f"{key.package}: {exception.expires_on.isoformat()}"
            )
        else:
            covered_no_fix.add(key)
    for key in sorted(exceptions.keys() - no_fix.keys()):
        errors.append(f"stale exception for {key.severity} {key.vulnerability_id} {key.package}")

    counts = {
        severity: sum(key.severity == severity for key in findings)
        for severity in BLOCKING_SEVERITIES
    }
    summary = [
        "# Artifact vulnerability policy",
        "",
        f"- Result: `{'FAIL' if errors else 'PASS'}`",
        f"- Scan status: `{status}`; continuous analysis: `{continuous}`",
        f"- CRITICAL findings: {counts['CRITICAL']}",
        f"- HIGH findings: {counts['HIGH']}",
        f"- Findings with a fix: {len(fixable)}",
        f"- No-fix findings covered by current exceptions: {len(covered_no_fix)}",
    ]
    if covered_no_fix:
        summary.extend(["", "## Active no-fix exceptions", ""])
        for key in sorted(covered_no_fix):
            exception = exceptions[key]
            summary.append(
                f"- `{key.severity} {key.vulnerability_id} {key.package}` through "
                f"`{exception.expires_on.isoformat()}` ([tracking issue]({exception.tracking_issue}))"
            )
    if errors:
        summary.extend(["", "## Blocking reasons", "", *[f"- {error}" for error in errors]])
    return errors, summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--exceptions", type=Path, required=True)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        scan = _load_json(args.scan)
        exception_payload = _load_json(args.exceptions)
    except ValueError as error:
        print(f"Artifact vulnerability policy input error: {error}")
        return 1
    errors, summary = evaluate(scan, exception_payload, today=args.today)
    print("\n".join(summary))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
