"""Deterministic, dependency-free completion document package generation."""

import io
import json
import zipfile
from collections.abc import Iterable

from app.modules.completion.schemas import CompletionSummaryView

PDF_LINES_PER_PAGE = 42
PDF_LINE_WIDTH = 72


def _wrapped(lines: Iterable[str]) -> list[str]:
    wrapped: list[str] = []
    for source in lines:
        value = source.strip()
        if not value:
            wrapped.append("")
            continue
        while len(value) > PDF_LINE_WIDTH:
            wrapped.append(value[:PDF_LINE_WIDTH])
            value = value[PDF_LINE_WIDTH:]
        wrapped.append(value)
    return wrapped


def _hex_text(value: str) -> str:
    return value.encode("utf-16-be").hex().upper()


def _pdf(title: str, lines: Iterable[str]) -> bytes:
    """Render text with a standard Korean CID font and no external dependency."""

    body_lines = _wrapped(lines)
    pages = [
        body_lines[index : index + PDF_LINES_PER_PAGE]
        for index in range(0, max(1, len(body_lines)), PDF_LINES_PER_PAGE)
    ]
    page_numbers = [5 + index * 2 for index in range(len(pages))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Count {len(pages)} /Kids "
            f"[{' '.join(f'{number} 0 R' for number in page_numbers)}] >>"
        ).encode(),
        (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /HYSMyeongJo-Medium "
            b"/Encoding /UniKS-UCS2-H /DescendantFonts [4 0 R] >>"
        ),
        (
            b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HYSMyeongJo-Medium "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Korea1) /Supplement 2 >> >>"
        ),
    ]
    for index, page_lines in enumerate(pages):
        page_number = page_numbers[index]
        content_number = page_number + 1
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
        ).encode()
        commands = [
            "BT",
            "/F1 16 Tf",
            "50 790 Td",
            f"<{_hex_text(title)}> Tj",
            "/F1 10 Tf",
            "0 -26 Td",
        ]
        for line in page_lines:
            commands.extend((f"<{_hex_text(line)}> Tj", "0 -15 Td"))
        commands.append("ET")
        stream = "\n".join(commands).encode()
        content = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        objects.extend((page, content))

    output = io.BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode())
        output.write(value)
        output.write(b"\nendobj\n")
    xref_offset = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n".encode())
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return output.getvalue()


def _zip_entry(name: str, payload: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info, payload


def build_completion_archive(summary: CompletionSummaryView) -> bytes:
    """Build the four documented PDFs plus a machine-readable manifest."""

    if not summary.archive_ready or summary.quote is None:
        raise ValueError("completion documents are not ready")
    quote_lines = [
        f"작업: {summary.job.job_code} / {summary.job.title}",
        f"승인 범위: {summary.approved_scope_version_label}",
        f"기본 금액: {summary.quote.base_amount_krw:,} KRW",
    ]
    quote_lines.extend(
        f"조정: {adjustment.label} {adjustment.amount_krw:+,} KRW"
        for adjustment in summary.quote.adjustments
    )
    quote_lines.append(f"최종 금액: {summary.quote.total_amount_krw:,} KRW")

    change_lines = [f"작업: {summary.job.job_code}"]
    if summary.field_changes:
        change_lines.extend(
            (
                f"{change.proposal_id} / {change.title} / {change.status} / "
                f"{change.amount_delta_krw:+,} KRW / 총 {change.total_amount_krw:,} KRW"
            )
            for change in summary.field_changes
        )
    else:
        change_lines.append("현장 변경 기록 없음")

    completion_lines = [
        f"작업: {summary.job.job_code}",
        f"완료 제출 ID: {summary.completion_submission_id}",
        f"작업 종료: {summary.completed_at.isoformat() if summary.completed_at else '-'}",
        f"실제 작업 시간: {summary.duration_minutes if summary.duration_minutes is not None else '-'}분",
        (f"체크리스트: {summary.checklist.completed_count}/{summary.checklist.total_count}"),
        f"현장 고객 확인: {'예' if summary.onsite_confirmation_completed else '아니오'}",
        f"완료 미디어 수: {summary.completion_media_count}",
    ]
    completion_lines.extend(
        (
            f"근무: {shift.display_name} ({shift.role_label}) / "
            f"{shift.started_at.isoformat()} - {shift.ended_at.isoformat()} / "
            f"{shift.duration_minutes}분"
        )
        for shift in summary.worker_shifts
    )
    completion_lines.extend(
        f"미디어: {media.media_asset_id} / {media.room_zone_label} / {media.content_type}"
        for media in summary.completion_media
    )

    request = summary.completion_request
    decision_lines = [f"작업: {summary.job.job_code}"]
    if request is None:
        decision_lines.append("고객 완료 확인 요청 없음")
    else:
        decision_lines.extend(
            (
                f"완료 확인 요청 ID: {request.completion_request_id}",
                f"요청 상태: {request.status.value}",
                f"요청 시각: {request.requested_at.isoformat()}",
                f"만료 시각: {request.expires_at.isoformat()}",
                (
                    "기록 외 추가금 응답: "
                    + (
                        "응답 안 함"
                        if request.unrecorded_extra_charge is None
                        else "예"
                        if request.unrecorded_extra_charge
                        else "아니오"
                    )
                ),
            )
        )
        if request.problem_report is not None:
            decision_lines.extend(
                (
                    f"문제 유형: {request.problem_report.problem_type.value}",
                    f"문제 설명: {request.problem_report.description}",
                    "이 기록은 원인이나 책임을 자동 판단하지 않습니다.",
                )
            )

    manifest = {
        "schema_version": 1,
        "job_id": str(summary.job.job_id),
        "job_code": summary.job.job_code,
        "scope_version_id": (
            str(summary.approved_scope_version_id)
            if summary.approved_scope_version_id is not None
            else None
        ),
        "completion_submission_id": (
            str(summary.completion_submission_id)
            if summary.completion_submission_id is not None
            else None
        ),
        "completion_request_id": (
            str(request.completion_request_id) if request is not None else None
        ),
        "document_names": [
            "01_견적서.pdf",
            "02_변경_승인_기록.pdf",
            "03_작업_완료_기록.pdf",
            "04_완료_확인_기록.pdf",
        ],
    }
    entries = (
        _zip_entry("01_견적서.pdf", _pdf("견적서", quote_lines)),
        _zip_entry("02_변경_승인_기록.pdf", _pdf("변경 승인 기록", change_lines)),
        _zip_entry("03_작업_완료_기록.pdf", _pdf("작업 완료 기록", completion_lines)),
        _zip_entry("04_완료_확인_기록.pdf", _pdf("완료 확인 기록", decision_lines)),
        _zip_entry(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(),
        ),
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as archive:
        for info, payload in entries:
            archive.writestr(info, payload)
    return output.getvalue()
