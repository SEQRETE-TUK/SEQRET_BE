# SEQRET MVP 프론트엔드 목표 API 제안서

> 상태: 목표 계약 문서. 구현으로 표시한 경로만 현재 실행 계약
>
> 기준일: 2026-08-15
>
> backend 기능 기준 코드: 이 문서를 포함한 최신 `main`
>
> frontend 확인 기준: `SEQRETE-TUK/SEQRET_FE` `origin/main`
> `16b4a98b812e798ad62942f0d82d5d6d7e715068`
>
> 실행 계약의 단일 원본: 최신 `main` 코드와 비운영 환경의 `/openapi.json`

## 1. 기준과 범위

이 명세의 제품 범위는 프론트엔드가 전달한 [MVP 와이어프레임](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/)이다.

검토한 업무 화면:

- [고객 화면 3개](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/?node-id=88-12): 작업범위 확인, 사진·AI 검토, 현장 변경 승인
- [업체 화면 3개](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/?node-id=88-176): 작업범위 검토·확정, 배차·인력 배정, 완료·변경 내역
- [현장기사 화면 2개](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/?node-id=88-691): 현장 상세·체크인, 변경·이슈 보고

2026-08-15에 확인한 [최신 frontend](https://github.com/SEQRETE-TUK/SEQRET_FE/tree/16b4a98b812e798ad62942f0d82d5d6d7e715068)는
Vite·React 19 기반이며, 자체 PRD가 소비자 12개·업체 mobile 6개·업체 web 4개·작업자 5개인
총 27개 demo 화면을 선언한다. 실제 source에는 역할 진입, 소비자 `screen=1..15`, 업체 mobile
`screen=0..5`, 업체 web `view=cases|quote|assign|operate`, 작업자 `screen=0..4`와 다수의
`state` query variant가 있다. 이 수치는 시각 demo 범위이며 backend E2E 완료 수가 아니다.

frontend source에는 `VITE_API_BASE_URL`, `/api/v1`로 제한한 공통 client, 명시적 Bearer 전달,
opaque signed PUT helper와 TanStack Query provider·retry 정책이 있다. `/consumer/capture`는 이
기반으로 촬영·upload·분석 완료, AI 초안 조회·편집·완료와 `409` 최신 상태 복구까지 실제
query·mutation을 수행한다. 나머지 demo 화면 전이는
여전히 component `useState`와 timer로 실행된다. frontend PRD 부록의 `/v1/jobs`,
`consumer|provider|crew`, 공통 error envelope와 CRUD 경로도 현재 backend 계약이 아니라 별도
제안이다. 이 문서의 경로와 frontend PRD 경로를 암묵적으로 선택하거나 혼용하지 않는다.

이 문서의 17개 화면 경로 중 `analysis-review` 조회·완료 2개, `scope-review` 조회·제안·수정요청·확인
4개, 변경 제안 조회·결정 2개와 현장 이슈 보고가 현재 OpenAPI에 등록됐다. 업체 이슈 목록·변경
제안·설명 3개와 소비자 onboarding·역할 초대 8개 operation도 별도 실행 계약으로 등록됐다. 나머지는 제품
범위와 A 소유 업무 계약을 확정한 뒤 계약 PR, 구현, 권한·중복 호출 test와 OpenAPI 반영을 마쳐야
frontend 실행 계약이 된다.
B 소유 Port·event·AI 결과 schema가 바뀌는 slice에만 해당 계약 영향을 별도로 조정한다.
이 초안만으로 client를 생성하거나 현재 route를 제거하지 않는다.

다음 원칙으로 목표 API를 제안한다.

1. 화면의 표시 항목을 채우는 조회 API만 둔다.
2. 화면의 버튼과 제출 행동마다 하나의 command API만 둔다.
3. 여러 내부 table을 그대로 노출하는 CRUD 대신 화면 단위 response를 사용한다.
4. 화면에 없는 운영·정합성·감사·background job API는 frontend contract에서 제외한다.
5. 전화, 채팅과 길 안내는 외부 앱 URI로 처리하고 별도 backend API를 만들지 않는다.

기존 8개 화면을 위한 제안 API는 **17개**다. 추가 P0 화면 2개의 승인 여부에 따라 19개로
확장할 수 있다. 시스템 운영 endpoint 3개는 별도이며 frontend가 호출하지 않는다.

## 2. 공통 규약

### 2.1 URL·인증

- 기본 prefix: `/api/v1`
- 인증: `Authorization: Bearer <access-link-secret>`
- 고객: `customer`
- 업체 담당자: `company_manager`
- 현장기사: `field_worker`
- 작업 ID와 인증 actor의 작업이 다르면 정보 노출 방지를 위해 `404`를 반환한다.
- 후속 목표 계약의 mutation은 중복 탭과 mobile retry를 막기 위해 `Idempotency-Key` header를 받는다. 같은 key와 같은 body는 기존 결과를 반환한다. 현재 main의 HTTP API에는 이 공통 header 계약이 없다. 구현된 `analysis-review` 완료와 범위 제안·수정요청·확인은 현재 source ID와 정확한 payload를 기준으로 재전송을 식별한다. 현장 이슈는 작업별 `client_reference`와 정확한 payload, 변경 제안은 이슈 ID와 정확한 payload, 결정·설명은 기존 terminal 상태와 정확한 payload로 replay를 식별한다.

MVP에서는 인증하는 현장기사 한 명을 대표 현장 사용자로 본다. 팀장을 포함한 배차 화면의 작업자들은 로그인 participant가 아니라 업체의 배정 인력 record다.

### 2.2 값 표현

- ID: UUID 문자열
- 시각: timezone offset이 포함된 ISO 8601
- 금액: 원 단위 정수 `*_amount_krw`. client가 쉼표와 `원`을 표시한다.
- 비율: `0.0..1.0`; client가 백분율로 표시한다.
- 주소: 화면에는 마스킹하거나 구·동 단위로 축약한다.
- 미디어 preview URL: 짧은 만료시간의 signed URL이며 응답과 로그를 cache하지 않는다.

### 2.3 공통 오류

```json
{"detail": "scope proposal state conflict"}
```

| Status | 의미 |
| --- | --- |
| `400` | 허용하지 않는 command 조합 |
| `401` | token 누락·만료·철회 |
| `403` | 역할 권한 부족 |
| `404` | 리소스 없음 또는 다른 작업의 리소스 |
| `409` | version·상태·배정 충돌 또는 중복 command |
| `422` | schema, 증거 미디어 또는 업무 입력 검증 실패 |
| `429` | 요청 제한 초과. `Retry-After` 포함 |
| `503` | DB 또는 storage provider 사용 불가 |

모든 JSON 응답에는 `x-request-id` header가 포함된다. token, signed URL, 전화번호, 주소 원문과 미디어 원본은 로그에 기록하지 않는다.

## 3. 제안 API 목록

### 3.0 소비자 onboarding·역할 초대 실행 계약

| Method | Path | 용도 | 인증 | 구현 상태 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/v1/move-jobs/onboarding` | 작업과 고객 참여자·고객 전용 capability 하나 생성 | 공개 | 구현 |
| `GET` | `/api/v1/me` | 현재 역할·초대 상태·명시적 permission 조회 | 모든 link | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/invitations` | 다음 역할 초대와 one-time secret 발급 | 고객, 업체 | 구현 |
| `GET` | `/api/v1/move-jobs/{job_id}/invitations` | 본인이 발급했거나 받은 초대 목록 | 고객, 업체 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/accept` | 받은 초대 수락 | pending 초대 대상 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/decline` | 받은 초대 거절·link 철회 | pending 초대 대상 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/revoke` | 발급한 초대·하위 초대 철회 | 발급자 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/reissue` | 기존 link·하위 초대 철회 후 재발급 | 발급자 | 구현 |

요청은 `title`, timezone이 포함된 선택적 `scheduled_at`, `customer_display_name`, 1~2개의
`locations`를 받는다. 각 location은 중복되지 않는 `origin|destination`, 표시용 `label`과
1~100개의 고유한 `room_zones`를 가진다. 응답은 `job`과 `customer_access_link`만 반환하며
업체·현장기사 참여자나 secret을 미리 만들지 않는다. secret 응답은 `Cache-Control: no-store`다.
초대 순서는 소비자→업체, 수락 업체→현장기사로 고정한다. 상태는
`pending|accepted|declined|expired|revoked`이며 pending token은 `/me`와 자기 수락·거절 외 업무
API에서 401이다. 하위 link 만료는 발급자 만료를 넘지 않고 상위 초대 폐기·재발급 또는 발급자
access-link 직접 철회는 하위 초대를 함께 철회한다. 기존 `POST /move-jobs`의 세 역할 동시 생성은 신뢰 bootstrap 호환 계약이며 일반
frontend onboarding에서 호출하지 않는다. secret 응답은 `Cache-Control: no-store`이며 frontend는
query string, 영구 저장소, log와 analytics에 기록하지 않는다.

### 3.1 고객·업체 공통 범위

| Method | Path | 용도 | 역할 | 구현 상태 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/move-jobs/{job_id}/scope-review` | 작업범위, 금액, 공간별 항목, 원본 사진과 양측 확인 상태 조회 | 고객, 업체 | 구현·범위 v1 |
| `POST` | `/api/v1/move-jobs/{job_id}/scope-review/revision-request` | 고객의 `수정 요청` 제출 | 고객 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/scope-review/confirm` | 고객의 `이 범위 확인` 처리 | 고객 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/scope-proposals` | 업체가 수정 범위·금액·사유를 고객에게 전송 | 업체 | 구현·범위 v1 |

### 3.2 고객 사진·AI 검토

| Method | Path | 용도 | 역할 | 구현 상태 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/move-jobs/{job_id}/analysis-review` | 공간별 업로드 수, 실패 수, AI 항목·신뢰도 조회 | 고객 | 구현·현재 AI schema v1 범위 |
| `POST` | `/api/v1/move-jobs/{job_id}/analysis-review/complete` | 수정·직접 추가를 반영한 최종 AI 검토 결과 제출 | 고객 | 구현·불변 고객 편집본 생성 |

### 3.3 고객 현장 변경 승인

| Method | Path | 용도 | 역할 | 구현 상태 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/v1/move-jobs/{job_id}/change-proposals` | 현장 이슈를 범위·견적 변경안으로 전환 | 업체 | 구현 |
| `GET` | `/api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}` | 사유, 증빙 사진, 기존·추가·최종 금액과 기록 정보 조회 | 고객, 업체 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/decision` | 승인, 거절 또는 설명 요청 | 고객 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/explanation` | 고객 설명 요청에 업체 설명 제출 | 업체 | 구현 |

### 3.4 업체 배차·인력

| Method | Path | 용도 | 역할 | 구현 상태 |
| --- | --- | --- | --- | --- |
| `POST` | `/api/v1/move-jobs/{job_id}/dispatch/setup` | 신뢰된 업체 연동이 작업별 요구사항·후보 snapshot 등록 | 업체 | 구현·연동 지원 경계 |
| `GET` | `/api/v1/move-jobs/{job_id}/dispatch` | 요구 자원, 차량·작업자 후보, 충돌과 현재 선택 조회 | 업체 | 구현 |
| `PUT` | `/api/v1/move-jobs/{job_id}/dispatch` | 배정을 원자적으로 확정하고 대상자에게 알림 | 업체 | 구현 |

### 3.5 업체 완료·문서

| Method | Path | 용도 | 역할 | 구현 상태 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/move-jobs/{job_id}/completion-summary` | 완료 사진, 체크리스트, 근무 기록, 변경·금액·문서 요약 조회 | 업체 | 기존 완료·감사 data 재구성 필요 |
| `POST` | `/api/v1/move-jobs/{job_id}/completion-requests` | 고객에게 완료 확인 요청 전송 | 업체 | 미구현 |
| `GET` | `/api/v1/move-jobs/{job_id}/documents/archive` | 화면에 표시된 증빙 PDF를 ZIP으로 일괄 다운로드 | 업체 | 미구현 |

### 3.6 현장기사

| Method | Path | 용도 | 역할 | 구현 상태 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/move-jobs/{job_id}/field-brief` | 승인 범위, 일정, 마스킹 경로, 담당자, 현장 조건과 배정 자원 조회 | 현장기사 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/check-ins` | checklist 전체 확인과 `현장 도착 체크인` 기록 | 현장기사 | 구현 |
| `POST` | `/api/v1/move-jobs/{job_id}/media-uploads` | 이슈 증빙 사진의 signed upload URL 발급 | 현장기사 | storage logic 재사용·path 단순화 |
| `POST` | `/api/v1/move-jobs/{job_id}/field-issues` | 범위 밖 작업, 파손 위험 또는 현장 장애를 업체에 보고 | 현장기사 | 구현 |
| `GET` | `/api/v1/move-jobs/{job_id}/field-issues` | 이슈와 변경 제안 처리 상태 목록 | 업체, 현장기사 | 구현 |

## 4. 화면 조회 계약

화면 조회는 frontend가 여러 일반 CRUD를 조합하지 않도록 필요한 data를 한 응답에 제공한다.

### 4.1 공통 `JobHeader`

| Field | Type | 용도 |
| --- | --- | --- |
| `job_id` | UUID | 작업 ID |
| `job_code` | string | `MOVE-240912` 형태 표시용 code |
| `scheduled_at` | datetime | 작업 예정 시각 |
| `customer_display_name` | string | 업체 화면의 고객명 |
| `company_display_name` | string | 업체명 |
| `viewer_display_name` | string | 현재 사용자 표시명 |
| `origin_summary` | string | `마포` 또는 마스킹된 출발지 |
| `destination_summary` | string | `성동` 또는 마스킹된 도착지 |
| `viewer_role` | enum | 현재 화면 역할 |
| `workflow_stage` | enum | sidebar와 상태 표시 |
| `workflow_steps[]` | `{key, status}[]` | 업체 sidebar의 완료·현재·대기 상태 |
| `unread_notification_count` | integer | 업체 header의 `알림 3`; 업체 응답에서만 사용 |

별도 알림 목록 화면이 없으므로 notification list/read API는 만들지 않는다.

### 4.2 `GET /scope-review`

Response `ScopeReviewView`:

| Field | Type | 와이어프레임 표시 |
| --- | --- | --- |
| `job` | `ScopeReviewJobHeader` | 작업 ID·표시 code·제목·고객·업체·현재 역할·일정·출발/도착 label |
| `scope.id` | UUID | 확인 command 대상 |
| `scope.version_label` | string | `v3` |
| `scope.status` | enum | `company_review`, `customer_review`, `revision_requested`, `confirmed` |
| `scope.item_count` | integer | 물품 34개 |
| `scope.work_count` | integer | 작업 8개 |
| `scope.exclusion_count` | integer | 제외 2개 |
| `scope.review_required_count` | integer | 검토 필요 2개 |
| `scope.room_groups[]` | `RoomScopeGroup[]` | 거실·침실·주방별 항목과 상태 |
| `scope.included_works[]` | string[] | 포장, 운반, 조립 등 |
| `scope.exclusions[]` | string[] | 에어컨 이전, 귀중품 등 |
| `proposal_id` | UUID/null | 현재 범위에 연결된 업체 제안 |
| `quote` | `QuoteSnapshot`/null | 기본·추가·제안 총액 |
| `proposal_reason` | string/null | 업체 변경 사유 |
| `media_previews[]` | `MediaPreview[]` | 공간별 원본 사진 |
| `company_confirmed_at` | datetime/null | 업체 상태 |
| `customer_confirmed_at` | datetime/null | 고객 상태 |
| `revision_request` | object/null | 수정 요청 ID·대상·사유·요청/해결 상태와 시각 |

`RoomScopeGroup`은 `room_zone_id`, `label`, `item_count`, `review_required_count`, `items[]`를 가진다.
각 item은 `item_key`, `room_zone_id`, `description`, `review_required`,
`source_media_asset_ids`를 반환한다. `MediaPreview`는 READY media만 대상으로
`media_asset_id`, `room_zone_id`, `content_type`, 5분짜리 generation-pinned `read_url`,
`expires_at`을 반환한다. object key, generation, model·prompt·provider 값은 응답에 포함하지 않는다.
현재 범위 v1은 항목 key·공간·설명만 지원하며 수량·단위·작업 메모는 AI schema v2 이후 확장한다.

### 4.3 `GET /analysis-review`

Response `AnalysisReviewView`:

| Field | Type | 용도 |
| --- | --- | --- |
| `job` | `JobHeader` | mobile header |
| `analysis_run_id` | UUID | 검토 완료 command 대상 |
| `status` | enum | `ready_for_review`, `completed` |
| `upload_groups[]` | `{room_zone_id, label, uploaded_count}` | 거실 5·침실 4·주방 3 |
| `failed_upload_count` | integer | 실패 0장 |
| `original_retained` | boolean | 원본 보관 중 |
| `detected_count` | integer | AI 분석 34개 |
| `review_required_count` | integer | 검토 필요 2개 |
| `items[]` | `AnalysisReviewItem[]` | 품목·수량·신뢰도·검토 상태 |

`AnalysisReviewItem`: `item_key`, `room_zone_id`, `name`, `quantity`, `unit`, `work_note`, `confidence`, `review_required`, `source_media[]`.

화면의 수정과 직접 추가는 client local state로 처리한다. 중간 저장 API는 만들지 않고 `AI 검토 완료` 때 최종 `items[]`를 한 번 제출한다.

### 4.4 `GET /change-proposals/{proposal_id}`

Response `ChangeProposalView`:

- `job: JobHeader`
- `proposal_id`, `field_issue_id`, `status`, `title`, `reason`
- `base_scope_version_id`, `base_scope_version_label`, `result_scope_version_id`
- `evidence_media: MediaPreview[]`
- `quote: QuoteSnapshot`
- `requested_by`, `requested_at`
- `clarification_note`, `clarification_requested_at`
- `explanation`, `explained_at`
- `decided_by`, `decided_at`, `decision_note`

`requested_by`와 `decided_by`는 참여자 ID, 표시명과 역할만 반환한다. 증거 preview는 `READY`
객체의 generation-pinned 5분 URL이고 object key·generation은 포함하지 않는다.

### 4.5 `GET /dispatch`

Response `DispatchView`:

- `job: JobHeader`
- `setup_id`, `dispatch_id`, `source_scope_version_id`, `source_scope_version_label`
- `requirements`: 시작 시각, 예상 시간, 차량 수·용량, 작업자 수, 필수 기술·자격
- `vehicle_options[]`: 차량 ID, 표시 번호, 규격, 설비, 용량, 가용 상태
- `worker_options[]`: 작업자 ID, 이름, 역할, 기술, 자격, 일정 상태와 선택적 대표 participant ID
- `selected_vehicle_id`, `selected_worker_ids`, `lead_worker_id`
- `checks`: 차량 일정·용량, 작업자 일정·인원, 필수 기술, 필수 자격
- `worker_note`, `status: setup_required|ready|stale|confirmed`, `confirmed_at`, `notification_created`

차량·작업자 master CRUD는 이 와이어프레임 범위가 아니다. `POST /dispatch/setup`은 업체 내부
resource provider가 만든 작업별 후보와 요구사항을 immutable snapshot으로 넘기는 연동 경계다.
작업 일정은 server의 `move_job.scheduled_at`을 사용하며 같은 `client_reference`와 정확히 같은 payload
재전송만 기존 setup을 반환한다. 현재 잠긴 leaf 범위가 바뀌면 조회 상태는 `stale`이고 확정은 거부된다.

### 4.6 `GET /completion-summary`

Response `CompletionSummaryView`:

- `job: JobHeader`
- `completed_at`, `final_amount_krw`, `duration_minutes`
- `completion_media[]`, `completion_media_count`
- `checklist: {completed_count, total_count}`
- `onsite_signature_completed`
- `worker_shifts[]`: 작업자, 역할, 시작·종료, 근무시간
- `field_changes[]`: 제목, 승인 시각, 증감 금액
- `quote: QuoteSnapshot`
- `completion_request_status: not_requested|requested`
- `completion_requested_at`
- `approved_scope_version_label`
- `documents[]`: 문서명, 생성 상태
- `retention_until`, `problem_report_count`

와이어프레임에 고객의 완료 확인 수신 화면이 없으므로 이번 contract는 요청 전송까지만 정의한다. 고객 확인 action은 해당 화면이 추가되기 전에는 API로 만들지 않는다.

`GET /documents/archive`는 `200 application/zip`과 `Content-Disposition` 파일명을 반환한다. 화면의 필수 문서가 아직 준비되지 않았으면 빈 archive 대신 `409`를 반환한다.

### 4.7 `GET /field-brief`

Response `FieldBriefView`:

- `job: JobHeader`
- `dispatch_id`, `scope_version_id`, `scope_version_label`
- `start_at`, `masked_origin`, `masked_destination`
- `lead_worker_name`, `lead_worker_call_uri`, `company_chat_uri`
- `origin_conditions[]`, `field_check_required_count`
- `assigned_vehicle`, `assigned_worker_count`, `required_skills[]`
- `safety_notice`, `navigation_uri`
- `checked_in_at`

현재 저장소에는 신뢰된 전화, 채팅과 navigation source가 없으므로 세 URI는 모두 `null`이다. 이후
provider가 연결되더라도 접근 권한을 확인한 현장기사에게만 반환하며 로그에는 남기지 않는다.

## 5. command 계약

### 5.1 범위 수정 요청

`POST /scope-review/revision-request`

```json
{
  "scope_version_id": "uuid",
  "reason": "붙박이장 수량을 다시 확인해 주세요."
}
```

- `reason`: 1..2000자
- 성공: `201`, `{revision_request_id, scope_proposal_id, scope_version_id, status, reason, requested_at, resolved_by_scope_proposal_id, resolved_at}`
- 현재 고객 확인 대기 version에만 요청할 수 있다.
- 같은 참여자가 같은 version과 reason을 재전송하면 기존 요청을 반환하고 다른 payload나 stale version은 `409`다.

### 5.2 범위 확인

`POST /scope-review/confirm`

```json
{"scope_version_id": "uuid"}
```

- 성공: `200`, `{proposal_id, scope_version_id, status: "confirmed", confirmed_at}`
- 업체가 전송한 현재 version만 확인할 수 있다.
- 같은 고객 확인의 재전송은 기존 확인 시각을 반환한다. 고객 확인은 기존 업체 확인과 합쳐 해당 범위를 잠그고 `scope_locked.v1`을 기록한다.

### 5.3 AI 검토 완료

`POST /analysis-review/complete`

```json
{
  "analysis_run_id": "uuid",
  "items": [
    {
      "item_key": "living-sofa-1",
      "room_zone_id": "uuid",
      "name": "3인 소파",
      "quantity": 1,
      "unit": "개",
      "work_note": "일반 운반",
      "source_media_asset_ids": ["uuid"]
    }
  ]
}
```

- `items`: 1..500개, `item_key` 중복 불가
- 성공: `200`, `{scope_draft_id, status: "company_review"}`
- AI confidence는 원본 분석 기록에 보존하고 고객 수정값을 AI 결과로 덮어쓰지 않는다.

### 5.4 업체 범위·금액 제안 전송

`POST /scope-proposals`

```json
{
  "source_scope_version_id": "uuid",
  "content": {
    "schema_version": 1,
    "items": [
      {
        "item_key": "living-sofa-1",
        "room_zone_id": "uuid",
        "description": "3인 소파 일반 운반"
      }
    ]
  },
  "included_works": ["포장", "운반"],
  "exclusions": ["에어컨 이전"],
  "quote": {
    "base_amount_krw": 1160000,
    "adjustments": [
      {"label": "피아노 추가 인력", "amount_krw": 120000}
    ],
    "total_amount_krw": 1280000
  },
  "reason": "피아노 안전 운반을 위해 작업자 1명 추가가 필요합니다."
}
```

- `content.items`: 1..500개, `item_key` 중복 불가. 현재 schema version은 1이다.
- `included_works`와 `exclusions`: 각각 최대 100개, 내부 중복과 양쪽 겹침 불가.
- server는 `base + sum(adjustments) == total`과 adjustment label 고유성을 검증한다.
- 최초 제안은 고객이 만든 현재 scope version을 source로 사용한다.
- 수정 제안은 고객이 수정 요청한 제안의 현재 result version을 source로 사용한다.
- 전송 자체가 업체 확인을 의미하고 새 불변 자식 scope version을 생성한다.
- 성공: `201`, `{proposal_id, proposal_kind, status, source_scope_version_id, result_scope_version_id, quote, included_works, exclusions, reason, sent_at, confirmed_at}`
- 같은 업체가 같은 source와 정확히 같은 payload를 재전송하면 기존 제안을 반환한다. 다른 payload, 과거 source, 수정 요청 없는 재제안은 `409`다.

### 5.4.1 업체 현장 변경 제안

`POST /change-proposals`

```json
{
  "field_issue_id": "uuid",
  "base_scope_version_id": "uuid",
  "title": "사다리차 추가",
  "reason": "도착지 엘리베이터 고장으로 외부 운반이 필요합니다.",
  "proposed_content": {
    "schema_version": 1,
    "items": [{"item_key": "ladder-truck", "room_zone_id": "uuid", "description": "사다리차 운반"}]
  },
  "quote": {
    "base_amount_krw": 1050000,
    "adjustments": [{"label": "사다리차", "amount_krw": 180000}],
    "total_amount_krw": 1230000
  }
}
```

- 업체만 제안할 수 있고 현장 이슈 하나에는 제안 하나만 연결한다.
- 기준 범위는 현재 잠긴 leaf여야 한다. `quote.base_amount_krw`는 현재 확정 견적 또는 앞서 승인된 변경 제안 최종 금액과 정확히 같아야 한다.
- 제안 내용은 기준 범위와 달라야 하고 견적 합계 불변식을 만족해야 한다.
- 성공: `201`, `{proposal_id, field_issue_id, status, base_scope_version_id, result_scope_version_id, quote, requested_at}`
- 같은 이슈와 정확히 같은 payload 재전송만 기존 제안을 반환한다. 이슈 재사용, stale 범위나 상충 payload는 `409`다.

### 5.5 현장 변경 결정

`POST /change-proposals/{proposal_id}/decision`

```json
{
  "decision": "approve",
  "note": null
}
```

- `decision`: `approve`, `reject`, `request_clarification`
- `reject`와 `request_clarification`은 `note` 1..2000자 필수
- 승인하면 제안 내용을 새 scope version으로 저장하고 업체·고객 확인과 잠금을 같은 transaction에서 완료한다.
- 성공: `200`, `{proposal_id, status, result_scope_version_id, clarification_requested_at, decided_at}`
- 같은 terminal 결정 또는 설명 요청의 정확 재전송은 기존 결과를 반환하고 상충 결정은 `409`다.
- 설명 요청 뒤 업체는 `POST /change-proposals/{proposal_id}/explanation`에 1..2000자 `explanation`을 보내며 같은 설명 replay만 허용한다.

### 5.6 배정 확정

업체 연동은 배정 화면을 열기 전에 `POST /dispatch/setup`으로 다음 snapshot을 한 번 등록한다.

```json
{
  "client_reference": "uuid",
  "source_scope_version_id": "uuid",
  "expected_duration_minutes": 180,
  "required_vehicle_capacity_m2": 25,
  "required_worker_count": 2,
  "required_skills": ["대형가구", "포장"],
  "required_certifications": ["화물운송"],
  "check_in_items": [{"key": "safety", "label": "안전 장비 확인"}],
  "origin_conditions": ["엘리베이터 사용 가능"],
  "safety_notice": "고객 확인 전 운반을 시작하지 않습니다.",
  "vehicles": [{
    "external_reference": "vehicle-12ga3456",
    "display_name": "1톤 윙바디 12가3456",
    "specification": "적재 30m2",
    "equipment": ["리프트"],
    "capacity_m2": 30,
    "available": true,
    "conflict_reason": null
  }],
  "workers": [
    {
      "external_reference": "worker-lead",
      "display_name": "현장 리더",
      "role_label": "팀장",
      "skills": ["대형가구"],
      "certifications": ["화물운송"],
      "available": true,
      "conflict_reason": null,
      "participant_id": "uuid"
    },
    {
      "external_reference": "worker-helper",
      "display_name": "포장 보조",
      "role_label": "보조",
      "skills": ["포장"],
      "certifications": [],
      "available": true,
      "conflict_reason": null,
      "participant_id": null
    }
  ]
}
```

- `vehicles`와 `workers`는 각각 하나 이상이며 외부 reference가 중복될 수 없다. 비가용 후보는 `conflict_reason`이 필수다.
- 인증하는 현장기사 participant는 작업자 후보 정확히 하나에 연결한다. 요구 인원은 후보 수를 넘을 수 없다.
- 성공: `201 DispatchView`; 다른 snapshot으로 재등록하면 `409`다.

`PUT /dispatch`

```json
{
  "setup_id": "uuid",
  "vehicle_id": "uuid",
  "lead_worker_id": "uuid",
  "worker_ids": ["uuid", "uuid", "uuid", "uuid"],
  "worker_note": "피아노 이동 전 바닥 보강, 도착지 엘리베이터 상태 확인"
}
```

- server가 현재 잠긴 leaf 범위, 요구 인원, 중복 작업자, 차량 일정·용량, 작업자 일정, 필수 기술·자격과 대표 현장기사 포함 여부를 다시 검증한다.
- 검증과 저장이 모두 성공한 뒤 `dispatch_confirmed.v1`과 현장기사 알림 intent를 한 번만 생성한다.
- 성공: `200 DispatchView`; 정확히 같은 command 재전송은 같은 확정을 반환하고 다른 선택은 `409`다.

### 5.7 완료 확인 요청

`POST /completion-requests`

- body 없음
- 이미 요청한 경우 기존 결과를 반환한다.
- 성공: `201`, `{request_id, status: "requested", requested_at}`

### 5.8 현장 체크인

`POST /check-ins`

```json
{
  "dispatch_id": "uuid",
  "confirmed_check_keys": ["identity", "safety"]
}
```

- 서버 시각을 사용하며 setup에 등록된 checklist key 전체를 중복 없이 보내야 한다.
- 배정된 시작일 당일이며 확정 배차에 포함된 대표 현장기사만 호출할 수 있다.
- 성공: `201`, `{check_in_id, dispatch_id, participant_id, confirmed_check_keys, checked_in_at}`. 같은 key 집합의 재전송은 최초 시각을 반환한다.

### 5.9 증빙 upload URL

`POST /media-uploads`

```json
{
  "purpose": "field_issue",
  "content_type": "image/jpeg",
  "content_length": 2458123
}
```

- 이 단순화 경로는 아직 구현되지 않았다. 현재 실행 계약은 현장기사가 자기 capture session을 만든 뒤
  기존 `/media-assets/upload`에서 `purpose: "change_evidence"`로 URL을 받고 opaque header를 적용해
  PUT한 다음 별도 `/complete` command를 호출하는 3단계다.
- 업로드 완료 뒤 `UPLOADED`부터 `POST /field-issues`가 해당 media ID를 받을 수 있다. 비동기
  validation이 generation·MIME type·크기·SHA-256을 확인해 모든 증거가 `READY`가 된 뒤에만 업체가
  변경 제안을 만들 수 있다. `PENDING_UPLOAD|PROCESSING|FAILED` 증거는 이슈 보고에서 거부한다.

### 5.10 현장 이슈 보고

`POST /field-issues`

```json
{
  "client_reference": "uuid",
  "base_scope_version_id": "uuid",
  "issue_type": "site_blocker",
  "title": "도착지 엘리베이터 고장",
  "description": "도착지 엘리베이터 고장으로 사다리차가 필요합니다.",
  "evidence_media_asset_ids": ["uuid"]
}
```

- `issue_type`: `out_of_scope`, `damage_risk`, `site_blocker`
- `title`: 1..200자, `description`: 1..2000자
- 증빙: 1..50장, 현재 현장기사가 촬영하고 upload complete를 마친 `UPLOADED|READY change_evidence`만 허용
- `base_scope_version_id`는 현재 잠긴 leaf여야 한다.
- 현장기사는 금액이나 고객 승인 결과를 입력할 수 없다.
- 성공: `201`, `{field_issue_id, client_reference, job_id, base_scope_version_id, issue_type, title, description, evidence_media_asset_ids, reported_by_participant_id, reported_at, status: "open", change_proposal_id}`
- 작업별 `client_reference`와 정확히 같은 payload 재전송은 기존 결과를 반환하고 다른 payload는 `409`다.

## 6. 핵심 data model

### 6.1 `ScopeLineV2`

현재 `ScopeItem v1`은 수량·구분·작업 정보를 표현하지 못하므로 와이어프레임용 schema v2가 필요하다.

| Field | Type | 제약 |
| --- | --- | --- |
| `item_key` | string | version 내부 고유, 1..100자 |
| `kind` | enum | `item`, `work`, `exclusion` |
| `room_zone_id` | UUID/null | 전체 작업·제외는 null 허용 |
| `name` | string | 1..200자 |
| `quantity` | integer/null | 품목이면 1 이상 |
| `unit` | string/null | `개`, `대`, `봉` 등 1..20자 |
| `work_note` | string/null | 포장·운반·분리 등 |
| `review_status` | enum | `confirmed`, `review_required` |
| `source` | enum | `ai`, `customer`, `company`, `field_change` |

불변 scope version은 `schema_version: 2`, `lines[]`, 포함·제외, `QuoteSnapshot`과 canonical content hash를 함께 저장한다.

### 6.2 `QuoteSnapshot`

| Field | Type | 제약 |
| --- | --- | --- |
| `base_amount_krw` | integer | 0 이상 |
| `adjustments[]` | array | `label`, signed `amount_krw` |
| `total_amount_krw` | integer | base + adjustment 합계, 0 이상 |

이 명세는 금액 합의 기록만 다룬다. 결제 승인, 카드 결제와 현금영수증 발행 API는 만들지 않는다.

### 6.3 `MediaPreview`

- `media_asset_id`
- `room_zone_label`
- `thumbnail_url`
- `expires_at`
- `content_type`

각 화면 조회가 필요한 preview를 묶어서 반환하므로 asset별 read URL endpoint는 만들지 않는다.

### 6.4 `AnalysisDraftItemV2`

현재 AI `DraftItem v1`은 화면의 수량과 작업 정보를 표현하지 못하므로 다음 필드가 필요하다.

- `item_key`, `room_zone_id`
- `name`, `quantity`, `unit`, `work_note`
- `confidence`, `review_required`
- `source_media_asset_ids[]`

AI 결과는 검토용 초안일 뿐이며 고객 검토와 업체 제안을 거치기 전에는 확정 scope나 금액을 변경하지 않는다.

### 6.5 현재 `AnalysisReview` v1 실행 계약

`GET /api/v1/move-jobs/{job_id}/analysis-review`는 호출 고객이 제출한 가장 최신 분석이
`completed`일 때만 성공한다. 더 최신 분석이 진행·실패 상태면 이전 결과로 자동 후퇴하지 않고
`409`를 반환한다.

- `source_scope_version_id`: AI import로 만든 불변 원본 version
- `review_scope_version_id`: 고객 검토 완료 전 `null`, 완료 후 불변 자식 version ID
- `zones[]`: 출발지 공간별 `total_media_count`, `ready_media_count`, `failed_media_count`
- `items[]`: 현재 편집 내용과 `source`, 선택적 `confidence`, `review_required`,
  `source_media_asset_ids`

model·prompt version, provider task ID, object key와 signed URL은 이 view에 포함하지 않는다.
`POST .../analysis-review/complete`는 `source_scope_version_id`와 최종 `items[]` 전체를 받는다.
현재 v1 item은 `item_key`, `room_zone_id`, `description`을 지원한다. 같은 고객이 같은 최종
내용을 재전송하면 기존 자식 version을 반환하고, stale 원본·상충 내용·다른 참여자가 만든 자식
version은 `409`다. 수량·단위·작업 메모는 `AnalysisDraftItemV2` 승인 뒤 확장한다.

## 7. 화면과 API 대응

| 와이어프레임 | 조회 | 화면 행동 → API |
| --- | --- | --- |
| 고객 작업범위 확인 | `GET /scope-review` | `수정 요청` → `revision-request`; `이 범위 확인` → `confirm` |
| 고객 사진·AI 검토 | `GET /analysis-review` | `AI 검토 완료` → `analysis-review/complete` |
| 고객 현장 변경 승인 | `GET /change-proposals/{id}` | `설명 요청 또는 거절`, `변경 승인하기` → `change-proposals/{id}/decision`의 decision 구분 |
| 업체 작업범위 검토·확정 | `GET /scope-review` | `수정안 고객에게 보내기` → `POST /scope-proposals` |
| 업체 배차·인력 배정 | `GET /dispatch` | `배정 확정 및 알림 발송` → `PUT /dispatch` |
| 업체 완료·변경 내역 | `GET /completion-summary` | `완료 확인 요청 보내기` → `POST /completion-requests`; `PDF 일괄 내려받기` → `GET /documents/archive` |
| 현장기사 상세·체크인 | `GET /field-brief` | `현장 도착 체크인` → `POST /check-ins`; 전화·채팅·길 안내는 신뢰 source가 없어 현재 비활성 |
| 현장기사 변경·이슈 보고 | `GET /field-brief`의 공통 context | `＋ 추가`는 기존 capture upload·complete 계약 사용; `업체에 이슈 보고` → `POST /field-issues`; `팀장에게 먼저 알리기`는 전화 URI |

## 8. frontend contract에서 제외한 항목

| 제외 항목 | 처리 방식 |
| --- | --- |
| 기존 세 역할 bootstrap과 운영용 자기 link 회전 | 일반 frontend는 onboarding·invitation 실행 계약을 사용한다. 기존 route는 신뢰 bootstrap·운영 호환 범위다. |
| 일반 작업·scope·change·completion CRUD/list | 화면 단위 view와 command로 대체 |
| notification 목록·읽음 처리 | 목록 화면이 없으므로 header count만 view에 포함 |
| audit event 전체 조회 | 화면에 필요한 변경·완료 기록만 completion summary에 포함 |
| background job·재처리·정합성·삭제 API | 내부 운영 기능. frontend에 노출하지 않음 |
| 차량·작업자 master CRUD | 업체 seed 또는 별도 연동 범위 |
| 결제·현금영수증 발행 | MVP 제외. 이미 생성된 문서만 표시·다운로드 |
| 자체 전화·채팅·지도 API | 신뢰 provider 계약 전에는 관련 URI를 `null`로 유지 |
| AI 실행 상태 polling·provider 세부 정보 | 화면에는 review-ready 결과만 제공 |
| 미디어 asset별 read URL API | 화면 view에 만료 preview URL 포함 |
| AI 수정 중간 저장 API | client local state 후 완료 시 한 번 제출 |

## 9. 현재 구현에서의 전환

현재 OpenAPI에는 46개 path와 54개 operation이 있다. `/api/v1` 업무 operation 51개와
운영 operation 3개다. 이 문서의 목표 17개 중 `analysis-review` 조회·완료 2개,
`scope-review` 조회·제안·수정요청·확인 4개, 변경 제안 조회·결정 2개, 현장 이슈 보고,
배차 조회·확정 2개와 field brief·체크인이 실행 계약으로 등록됐다. 배차 setup, 업체 이슈
목록·변경 제안·설명 3개와 소비자 onboarding·역할 초대 8개 operation도 별도 실행 계약으로
등록됐다. 나머지 제안 경로와 FE PRD 부록의 경로는 아직 등록되지 않았다.

| 현재 공개 경로 묶음 | 최종 처리 |
| --- | --- |
| `POST /move-jobs/onboarding` | 소비자 작업과 고객 capability 하나만 생성하는 공개 실행 계약이다. |
| `GET /me`, invitation 생성·목록·action | 역할별 landing, 단계적 초대와 link lifecycle의 공개 실행 계약이다. |
| `POST /move-jobs`, access-link 생성·철회 | 세 역할 동시 생성 bootstrap과 기존 운영 계약을 호환 유지하되 일반 frontend onboarding에서는 사용하지 않는다. |
| `GET /move-jobs/{id}` | 6개 화면 view에 필요한 header만 포함하고 제거 |
| capture session 생성·목록, asset upload·complete, submit·analysis status 6개 | storage와 durable 분석 흐름을 재사용한다. FE #4가 현재 계약으로 첫 E2E를 연결했으며, 이후 화면용 capture command/view로 축소할지는 별도로 결정한다. |
| analysis review 조회·완료 2개 | FE #5가 최신 완료 분석 조회·고객 검토 완료를 연결했고 FE #6이 `409` 최신 상태 복구를 보강했다. 검토 결과는 AI 원본의 불변 자식 scope version으로 한 번만 생성하며, 수량·단위·작업 메모는 AI schema v2 이후 확장한다. |
| scope review 조회·제안·수정요청·확인 4개 | FE 범위 화면용 실행 계약이다. 기존 scope version·approval을 내부 원본으로 재사용하며 범위 v1만 노출한다. |
| dispatch setup·조회·확정, field brief·check-in 5개 | 작업별 resource snapshot을 기준으로 배차를 확정하고 대표 현장기사에게 알림·브리프·당일 checklist 체크인을 제공한다. |
| scope version 생성·목록·approval | 신뢰 bootstrap·내부 호환 계약으로 남기고 일반 FE는 위 scope review 흐름을 사용한다. |
| change request 생성·목록·증거 read URL·설명·결정 | 호환 경로로 유지한다. 일반 FE는 역할을 분리하고 증거 preview를 묶은 `field-issues`와 `change-proposals` 실행 계약을 사용한다. |
| completion 확인 목록·audit 목록·notification 목록 | `completion-summary`와 header count로 통합 |
| background job 생성·목록·재시도 3개 | 내부 운영 기능으로 유지하고 frontend 계약에서 제외 |
| `/healthz`, `/edgez`, `/readyz` | 유지하되 운영 endpoint로 분류 |

현재 upload 계약은 opaque `upload_url`·`upload_headers`를 그대로 사용한 뒤 별도 complete command가
generation-pinned 비동기 검증을 시작한다. 이를 `media-uploads` 한 경로로 축소하려면 현재
`UPLOADED → PROCESSING → READY|FAILED` 계약과 재시도 의미를 보존하는 별도 설계가 필요하다.
현재 API CORS는 `GET`, `POST`, `PUT`만 허용하며 배차 확정의 browser preflight를 회귀 테스트한다.

domain의 불변 version, content hash, access control, Outbox, audit와 provider Port는 삭제하지
않는다. 이 문서만 병합해 현재 route를 deprecate하지 않으며, 승인된 교체 계약과 전환 계획이
OpenAPI에 반영된 뒤 frontend가 이동한다.

## 10. 제안 승인·구현 완료 기준

- 8개 화면의 모든 표시값이 해당 view response에 존재한다.
- 최신 FE 27개 demo 중 실제 MVP에 포함할 화면과 보조 state를 먼저 고정하고, 제외 화면은 mock임을 표시한다.
- 역할은 backend의 `customer`, `company_manager`, `field_worker`를 유지하거나 전환 계약을 별도 승인한다.
- base path는 `/api/v1`을 사용하며 FE PRD의 `/v1` 경로를 현재 계약처럼 호출하지 않는다.
- 모든 CTA와 제출 행동이 정확히 하나의 command endpoint에 대응한다.
- 화면에 없는 frontend endpoint가 OpenAPI frontend tag에 남지 않는다.
- 고객은 금액을 제안할 수 없고 현장기사는 금액·승인을 결정할 수 없다.
- 금액 snapshot 합계, scope version과 change proposal 연결을 server가 검증한다.
- signed URL, token, 전화번호와 주소 원문이 log·trace·error에 남지 않는다.
- mutation 중복 호출이 같은 업무 효과를 두 번 만들지 않는다.
- role 거부, 다른 작업 접근, stale version, 배정 충돌과 잘못된 증빙 test가 통과한다.
