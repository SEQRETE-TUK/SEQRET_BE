# SEQRET MVP API 구현 현황

> 기준일: 2026-08-12
>
> 기준 코드: `origin/main` `81a4dabaa72597332ac1c8aeb0287e2ccd84f376`
>
> 관련 문서: [API 명세](API_SPEC.md), [추가 화면 요청서](FRONTEND_SCREEN_REQUEST.md)

## 1. 한눈에 보기

| 구분 | 수량 | 의미 |
| --- | ---: | --- |
| 현재 FastAPI 등록 route | 23개 | 업무 route 20개 + 운영 route 3개 |
| 기존 와이어프레임 기준 frontend API | 17개 | 현재 Figma 8개 화면에 필요한 계약 |
| 추가 P0 화면 승인 시 frontend API | 19개 | 작업 완료 제출과 고객 완료 결정 2개 추가 |
| 최종 path와 schema 그대로 연동 가능 | 0개 | 현재 route는 도메인 기반 구현이며 화면 계약으로 재구성 전 |
| 기존 핵심 logic을 재사용할 API | 11개 | route 전환 2개 + 기능·응답 확장 9개 |
| 핵심 업무 상태부터 새로 만들 API | 8개 | 배차, 체크인, 완료 제출·요청, 문서 등 |

현재 route가 많다고 frontend 준비가 끝난 것은 아니다. frontend는 현재 20개 업무 route에 직접 연동하지 않고 [API 명세](API_SPEC.md)의 화면 단위 계약이 구현된 뒤 연동한다.

## 2. 상태 기준

| 상태 | 판단 기준 |
| --- | --- |
| `전환` | 핵심 application service가 있어 최종 route와 schema adapter가 주된 작업 |
| `확장` | 관련 model·service는 있으나 화면용 필드, 상태 전이 또는 권한 계약 추가 필요 |
| `신규` | 핵심 업무 record나 service가 아직 없음 |
| `조건부` | 제품 운영 방식이 확정될 때만 추가 |

## 3. 최종 frontend API 현황

### 3.1 기존 와이어프레임 17개

| 화면 | Method · Path | 용도 | 상태 | 재사용 기반·남은 일 |
| --- | --- | --- | --- | --- |
| 고객·업체 범위 | `GET /api/v1/move-jobs/{job_id}/scope-review` | 최신 범위, 금액, 사진, 양측 확인 상태 조회 | 확장 | 작업·범위 version 조회 재사용; 화면 view와 금액 snapshot 추가 |
| 고객 범위 | `POST /api/v1/move-jobs/{job_id}/scope-review/revision-request` | 고객 수정 요청 접수 | 신규 | 사전 범위 수정요청 record와 상태 전이 필요 |
| 고객 범위 | `POST /api/v1/move-jobs/{job_id}/scope-review/confirm` | 고객의 현재 범위 확인 | 전환 | 기존 `approve_scope_version` 재사용; 고객 전용 계약으로 노출 |
| 업체 범위 | `POST /api/v1/move-jobs/{job_id}/scope-proposals` | 범위·금액 제안을 고객에게 전송 | 확장 | 범위 version 생성 재사용; 수량·작업·금액·제안 상태 추가 |
| 고객 AI 검토 | `GET /api/v1/move-jobs/{job_id}/analysis-review` | 업로드와 AI 검토 초안 조회 | 확장 | 저장된 분석 원본과 media 재사용; 화면 view와 AI schema v2 필요 |
| 고객 AI 검토 | `POST /api/v1/move-jobs/{job_id}/analysis-review/complete` | 고객 수정 결과를 업체 검토 초안으로 제출 | 확장 | `import_analysis_draft` 재사용; HTTP command와 수정된 항목 계약 필요 |
| 고객 변경 승인 | `GET /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}` | 현장 변경 사유, 증빙, 금액 조회 | 확장 | 변경요청 record 재사용; 금액과 signed preview 조합 필요 |
| 고객 변경 승인 | `POST /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/decision` | 승인·거절·설명 요청 | 확장 | 기존 결정·설명 상태 전이 재사용; 고객 권한과 제안 계약으로 정리 |
| 업체 배차 | `GET /api/v1/move-jobs/{job_id}/dispatch` | 차량·작업자 후보와 충돌 조회 | 신규 | 차량, 작업자 자원, 가용성, 배정 model·service 필요 |
| 업체 배차 | `PUT /api/v1/move-jobs/{job_id}/dispatch` | 배정 확정과 알림 생성 | 신규 | 원자적 충돌 검증, 배정 저장과 알림 연결 필요 |
| 업체 완료 | `GET /api/v1/move-jobs/{job_id}/completion-summary` | 완료 사진, 근무, 변경, 금액, 문서 요약 | 확장 | 완료 확인·감사 기반 재사용; 작업자 제출·체크리스트·금액·문서 data 추가 |
| 업체 완료 | `POST /api/v1/move-jobs/{job_id}/completion-requests` | 고객에게 완료 확인 요청 | 신규 | 요청 lifecycle과 deep-link 알림 trigger 필요 |
| 업체 완료 | `GET /api/v1/move-jobs/{job_id}/documents/archive` | 증빙 PDF ZIP 다운로드 | 신규 | 문서 생성 상태와 archive 생성 service 필요 |
| 현장기사 범위 | `GET /api/v1/move-jobs/{job_id}/field-brief` | 최신 범위, 경로, 일정, 담당자와 현장 조건 조회 | 신규 | 기존 작업·범위에 배정·체크인 data를 합치는 view 필요 |
| 현장기사 범위 | `POST /api/v1/move-jobs/{job_id}/check-ins` | 현장 도착 시각 기록 | 신규 | 배정된 기사 검증과 check-in record 필요 |
| 현장기사 이슈 | `POST /api/v1/move-jobs/{job_id}/media-uploads` | 이슈 증빙 signed upload URL 발급 | 전환 | 기존 capture 3단계와 `StoragePort` 재사용; frontend path 단순화 |
| 현장기사 이슈 | `POST /api/v1/move-jobs/{job_id}/field-issues` | 범위 밖 작업·파손 위험·현장 장애 보고 | 확장 | 기존 변경요청·증빙 검증 재사용; 무가격 이슈와 업체 견적 단계를 분리 |

### 3.2 추가 P0 화면 승인 시 2개

| 화면 | Method · Path | 용도 | 상태 | 재사용 기반·남은 일 |
| --- | --- | --- | --- | --- |
| 현장기사 완료 기록 | `POST /api/v1/move-jobs/{job_id}/completion-submissions` | 완료 사진, 체크리스트, 실제 근무와 현장 확인 제출 | 신규 | 작업자 완료 제출 aggregate와 검증 필요 |
| 고객 완료 확인 | `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/decision` | 완료 확인 또는 문제 신고 | 확장 | 기존 완료 확인 logic 재사용; 요청 lifecycle과 문제 신고 상태 추가 |

두 API는 [추가 화면 요청서](FRONTEND_SCREEN_REQUEST.md)의 P0 화면이 승인되면 [API 명세](API_SPEC.md)에 편입한다. 승인 전 기준 API 수는 17개, 승인 후는 19개다.

### 3.3 조건부 API

| 조건 | 추가 API | 결정 기준 |
| --- | --- | --- |
| 고객·업체가 최초 사진을 직접 등록 | `POST /api/v1/move-jobs/{job_id}/capture-submissions` | 사진이 seed·관리자 과정에서 미리 준비되지 않을 때만 추가 |
| 업체가 작업과 고객 link를 직접 생성 | 작업 생성·초대 API | admin seed가 아닌 실제 업체 onboarding을 MVP에 넣을 때만 확정 |

## 4. 현재 서버 업무 route 20개

이 표는 현재 호출 가능한 API의 용도와 최종 처리 방향이다. `유지`는 frontend 공개 유지를 뜻하지 않고 내부 bootstrap·운영 용도로 보존한다는 의미다.

| 현재 Method · Path | 현재 용도 | 최종 처리 |
| --- | --- | --- |
| `POST /api/v1/move-jobs` | 작업과 초기 공간 생성 | private bootstrap으로 유지 |
| `GET /api/v1/move-jobs/{job_id}` | 작업·참여자·공간 구성 조회 | 화면별 view에 필요한 header만 재사용 |
| `POST /api/v1/move-jobs/{job_id}/participants` | 작업 참여자 연결 | private bootstrap으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/participants/{participant_id}/access-links` | 역할 link 재발급 | private bootstrap·운영으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/access-links/{access_link_id}/revoke` | 역할 link 철회 | private bootstrap·운영으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions` | 촬영 session 생성 | media upload 내부 logic으로 재사용 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/upload` | signed upload URL 발급 | `POST /media-uploads`로 축소 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/{media_asset_id}/complete` | 업로드 metadata 검증·확정 | 이슈·완료 제출 시 server 검증으로 흡수 |
| `POST /api/v1/move-jobs/{job_id}/scope-versions` | 불변 범위 version 생성 | `POST /scope-proposals` 내부 logic으로 재사용 |
| `GET /api/v1/move-jobs/{job_id}/scope-versions` | 범위 version 이력 조회 | `GET /scope-review` 화면 view로 교체 |
| `POST /api/v1/move-jobs/{job_id}/scope-versions/{scope_version_id}/approvals` | 고객·업체 범위 확인 | 고객 `POST /scope-review/confirm`으로 전환 |
| `POST /api/v1/move-jobs/{job_id}/change-requests` | 현장 변경요청 생성 | `field-issues`와 업체 `scope-proposals`로 단계 분리 |
| `GET /api/v1/move-jobs/{job_id}/change-requests` | 현장 변경요청 목록 조회 | 변경 상세와 완료 요약 view에 흡수 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/clarification` | 현장기사에게 설명 요청 | 최종 decision action으로 통합 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/explanation` | 현장기사 설명 제출 | 별도 화면 승인 전 frontend에서 제외 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/decision` | 현장 변경 승인·거절 | `change-proposals/{id}/decision`에 재사용 |
| `POST /api/v1/move-jobs/{job_id}/completion-confirmations` | 고객·업체 완료 확인 | 고객 완료 결정과 업체 완료 요청 흐름으로 재구성 |
| `GET /api/v1/move-jobs/{job_id}/completion-confirmations` | 완료 확인 이력 조회 | `completion-summary`에 흡수 |
| `GET /api/v1/move-jobs/{job_id}/audit-events` | 전체 감사 이력 조회 | 내부 유지; 화면에는 필요한 요약만 제공 |
| `GET /api/v1/move-jobs/{job_id}/notifications` | 참여자 알림 이력 조회 | 알림함 화면이 없어 frontend에서 제외 |

## 5. 운영 route 3개

| Method · Path | 용도 | frontend 사용 |
| --- | --- | --- |
| `GET /healthz` | process bootstrap 확인 | 사용하지 않음 |
| `GET /edgez` | 일반 public traffic 경로 확인 | 사용하지 않음 |
| `GET /readyz` | DB 연결 readiness 확인 | 사용하지 않음 |

## 6. 남은 backend 범위

### 계약·조회 재구성

- 화면 단위 view 6개: `scope-review`, `analysis-review`, `change-proposals/{id}`, `dispatch`, `completion-summary`, `field-brief`
- 공통 `JobHeader`, `ScopeLineV2`, `QuoteSnapshot`, signed `MediaPreview`
- AI `AnalysisResult` v2와 B consumer 영향 확인

### 신규 업무 상태

- 범위 수정요청
- 견적·금액 snapshot
- 배차·작업자·차량 가용성·체크인
- 작업자 완료 제출, 완료 확인 요청, 고객 결정·문제 신고
- 문서 생성 상태와 ZIP archive

### 예상 migration

배차·체크인, 견적, 완료 제출·요청·문제 신고와 문서 상태는 새 persistence가 필요하므로 migration 대상이다. 기존 범위 version, 승인, 변경요청, media, audit, notification table은 가능한 범위에서 재사용한다.

## 7. frontend 연동 기준

- 현재 route 이름으로 임시 연동하지 않는다.
- 각 최종 endpoint가 OpenAPI에 추가되고 권한·오류·중복 호출 test가 통과한 뒤 연동한다.
- 화면 조회는 여러 CRUD 호출을 조합하지 않고 화면별 `GET` 한 번을 기본으로 한다.
- CTA 하나는 command endpoint 하나에 대응한다.
- 추가 P0 화면 2개 승인 여부를 먼저 결정한 뒤 frontend API 기준 수를 17개 또는 19개로 고정한다.
