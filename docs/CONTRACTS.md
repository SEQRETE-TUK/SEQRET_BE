# 공통 계약

이 문서는 트랙 A와 B가 공유하는 provider-independent versioned 계약을 정의한다. 계약 구현은 `app/contracts`에 있고, 두 트랙은 상대 트랙의 ORM model이나 repository 대신 이 계약만 사용한다.

## 공통 원칙

- 모든 외부 경계 모델은 알 수 없는 필드를 거부하고 생성 후 변경할 수 없다.
- 식별자는 UUID 기반 nominal type이며, 시각은 timezone-aware 값만 허용한다.
- `ActorContext`는 인증 계층이 검증한 작업·참여자·역할 capability와 권한 경계를 application command에 전달한다. bearer 링크는 개인 신원을 증명하지 않으며 원문 역할 토큰은 포함하지 않는다.
- `DomainEvent.schema_version`의 최초 값은 `1`이다. 기존 event payload를 깨는 변경은 event 이름과 schema version을 새로 추가한다.
- signed URL, 역할 토큰, 주소 원문과 원본 미디어는 모델 repr이나 로그에 남기지 않는다.

## 역할 초대와 capability

- 공개 `POST /api/v1/move-jobs/onboarding`은 고객 참여자와 고객 access secret 하나만 발급한다. 기존 세 역할 동시 bootstrap은 일반 frontend onboarding에서 사용하지 않는다.
- 역할 위임 순서는 `customer → company_manager → field_worker`로 고정한다. 다른 역할 초대와 아직 수락하지 않은 발급자의 하위 초대는 거부한다.
- 초대 상태는 작업 상태와 분리된 `pending|accepted|declined|expired|revoked`다. pending token은 `/me`와 자신의 수락·거절 command에서만 인증되고 다른 업무 API에서는 유효한 참여자로 취급하지 않는다.
- `GET /api/v1/me`는 현재 link의 작업·참여자·역할, 표시명, 명시적 permission 목록, 만료와 선택적 초대 상태만 반환한다. secret과 token hash는 반환하지 않는다.
- 초대·재발급 응답의 평문 secret은 한 번만 반환하고 `Cache-Control: no-store`를 사용한다. DB에는 SHA-256 hash와 현재 link ID만 저장한다.
- 하위 초대 만료는 발급자 access link 만료를 넘지 않는다. 상위 초대를 폐기·재발급하거나 발급자의 access link를 직접 철회하면 그 참여자가 발급한 모든 하위 초대와 access link도 같은 transaction에서 철회한다.
- 고객의 `DELETE /api/v1/move-jobs/{job_id}`는 견적이 한 번도 생성되지 않은 작업만 `CANCELED`로 전이한다. 업무·감사 row를 물리 삭제하지 않고 모든 초대와 활성 access link를 같은 transaction에서 철회하며, link 철회 감사 payload의 `operation`은 `job_canceled`다.
- 수락 시에만 `PARTICIPANT_CONNECTED` 감사 event를 기록한다. 초대 landing 조회는 참여 완료로 기록하지 않으며, 감사 payload에는 invitation·link·participant ID와 역할만 남기고 secret은 넣지 않는다.
- `participant_invitation` 이력이 생긴 schema는 downgrade로 제거하지 않는다. rollback은 schema를 유지한 application revision 전환으로 수행한다.

## 작업공간 세션과 다중 작업

- 검증된 active 역할 link는 `POST /api/v1/sessions`에서 30일 서버 작업공간 계정과 세션에 연결한다. cookie 원문은 한 번만 발급하고 DB에는 SHA-256 hash만 저장한다.
- 하나의 작업공간 계정은 한 역할만 가진다. 같은 역할의 여러 작업 participant를 연결할 수 있지만 다른 역할 또는 이미 다른 active 계정이 소유한 participant 연결은 `409`다.
- 배포 cookie는 `HttpOnly; Secure; SameSite=None; Path=/api/v1`이다. `GET /api/v1/session`은 account와 active membership, 메모리 전용 CSRF token을 반환하며 cookie 원문은 반환하지 않는다.
- cookie 기반 unsafe method는 `X-SEQRET-CSRF`를 필수로 검증한다. bearer가 있으면 기존 capability 인증이 우선하며 CSRF header를 요구하지 않는다.
- `GET /api/v1/move-jobs`는 cookie account의 active membership 전체 또는 bearer participant 한 건만 조회한다. 목록은 상태·제목/표시명/위치 검색·예정일 구간·최대 100개 제한을 지원하고 secret이나 연락처를 포함하지 않는다.
- `PATCH /api/v1/move-jobs/{job_id}`는 고객만 견적 생성 전 호출한다. 제목, timezone-aware 예정시각 또는 `null`, 기존 출·도착지의 표시 label·구조화 조건만 부분 갱신하며 주소 원문과 room-zone topology는 변경하지 않는다. v2 범위가 있으면 조건 변경을 새 불변 자식 version에 snapshot하며 이미 잠긴 범위의 조건, 완료·취소 또는 견적 이력이 있으면 `409`다.
- 작업 기본정보 변경은 바뀐 필드 이름만 포함한 `JOB_BASIC_INFO_UPDATED` 감사 event를 append한다. 감사 row와 workspace 이력이 생긴 schema는 제거하지 않는다.

## Port 소유권과 동작

| Port | adapter 소유자 | 입력·출력 | 멱등성 | timeout | 오류 계약 |
| --- | --- | --- | --- | --- | --- |
| `StoragePort` (`ObjectStoragePort` 호환 별칭) | B | object key와 제약 → signed upload target, read URL, metadata, generation-pinned SHA-256 또는 삭제 완료 | 삭제는 `idempotency_key`로 중복 효과를 막는다 | 모든 provider 호출에 초 단위 명시 | provider 오류를 adapter 예외로 매핑하며 A ORM을 갱신하지 않는다 |
| `TaskQueuePort` | B | queue, handler, JSON payload → provider task ID | 같은 key는 한 task만 반환한다 | enqueue 호출에 초 단위 명시 | 재시도 가능 여부를 provider 외부 타입으로 노출하지 않는다 |
| `AIProviderPort` | B | `AnalysisRequest`의 분석·촬영·미디어 ID, 비주소 위치·구역 context, object key, model/prompt/result version → `AnalysisResult` | 같은 key는 같은 입력에 같은 분석 결과를 반환한다 | 분석 호출에 초 단위 명시 | 결과는 초안이며 `scope_version`을 생성하거나 잠그지 않는다 |
| `EventBusPort` | A | `DomainEvent` → 발행 완료 | event ID 기반 key로 중복 발행 효과를 막는다 | 발행 호출에 초 단위 명시 | Outbox 상태와 retry 정책은 A가 관리한다 |
| `CachePort` | A | namespace가 포함된 key와 fixed-window 길이 → 원자적으로 증가한 count | 같은 window의 증가가 기존 TTL을 연장하지 않는다 | cache 호출에 초 단위 명시 | Redis 오류는 adapter 예외로 매핑하며 application은 DB 원본 제한을 계속 적용한다 |
| `NotificationProviderPort` | 통합 INT-09 | 수신자별 정제된 `OutboundNotification` → provider request ID | delivery ID 기반 key를 전달한다. 알림톡은 provider가 10분 동안 중복 요청을 거부하고 Email·SMS grouping key는 추적용이다 | 개별 호출에 초 단위 명시 | provider HTTP·수신자 결과를 `ProviderError`와 재시도 가능 여부로 변환하며 secret·수신처를 로그에 남기지 않는다 |

로컬 fake는 실제 adapter와 같은 Protocol을 만족하고 멱등 동작을 contract test로 검증한다.

- `StoragePort.create_upload_url`은 객체가 없을 때만 생성하는 immutable `StorageUploadTarget`을 반환한다. 공용 모델은 provider별 URL과 header를 해석하거나 정규화하지 않는 opaque bag이며, API는 그 값을 `upload_url`과 `upload_headers`로 정확히 전달한다. 각 adapter가 provider별 create-only 조건을 서명하고 서명에 필요한 모든 header를 target에 넣는다. 현재 GCS adapter는 요청 `Content-Type`과 `x-goog-if-generation-match: 0`을 함께 서명·반환해야 한다.
- `StoragePort.create_read_url`은 DB에 검증·저장된 object generation을 필수로 받고, adapter는 그 generation을 signed URL에 고정한다. generation이 없는 미디어는 열람 URL 발급을 거부한다.
- `StoragePort.calculate_sha256`은 지정한 generation만 스트리밍해 소문자 SHA-256을 반환한다. 원본 전체를 메모리에 적재하거나 최신 generation으로 대체하지 않는다.
- `StoragePort.delete_object`의 generation은 1~255자의 필수 snapshot이다. snapshot generation이 이미 없으면 멱등 성공하고, 같은 key의 다른 generation 객체는 보존한다.
- 업로드 완료 command는 metadata의 object key, MIME type, 크기와 generation을 모두 검증하며, signed URL과 필수 header는 cache하거나 문자열을 정규화하지 않고 원문 그대로 전달한다.
- `PENDING_UPLOAD` 미디어는 실제 크기·hash·generation·업로드 시각을 갖지 않는다. 그 밖의 상태는 실제 크기, 비어 있지 않은 generation과 업로드 시각을 반드시 보존한다.
- `AnalysisRequest.source_media_asset_ids[n]`, `object_keys[n]`, `content_types[n]`은 같은 미디어를 가리킨다. 세 배열은 길이가 같고 ID와 object key 배열 안에서 중복이 없어야 한다. `content_types`는 A가 validation을 끝낸 `image/jpeg`, `image/png`, `video/mp4` 중 하나이며 B adapter는 확장자가 없는 object key에서 MIME type을 추정하지 않는다.
- v2 분석 요청은 `requested_result_schema_version=2`와 각 source에 정확히 대응하는 `source_contexts[]`를 사용한다. context에는 media asset, location, origin/destination kind와 room-zone ID만 포함하고 주소 원문은 포함하지 않는다.
- `AnalysisResult` v1은 기존 `description` 중심 품목만 유지한다. v2는 같은 품목에 `name`, `quantity`, `unit`, `work_note`를 구조화하고, 수량·단위가 불확실한 품목은 `review_required_items`에 함께 비운다. `draft_items`의 v2 품목은 수량·단위가 모두 있어야 한다.
- v2 `location_condition_suggestions[]`는 주거 형태, 층, 엘리베이터, 계단, 주차·진입, 운반거리와 접근 메모를 값 또는 명시적 `unknown`으로 전달한다. 각 제안은 source media ID, confidence와 `review_required_fields`를 보존하며 자동으로 작업 원본이나 확정 범위를 갱신하지 않는다.
- 모든 v2 품목과 위치 조건은 고객 검수 대상이다. B는 제안까지만 생성하고 A의 application command가 작업 topology와 source media를 다시 검증한 뒤 수정 가능한 범위 초안으로 가져온다.
- merge 순서는 이 계약과 fake → B의 Storage adapter rebase → 실제 provider 통합 검증이다. upload 반환형과 delete generation은 호환성을 깨는 계약 변경이므로 기존 B adapter를 먼저 병합하지 않는다.

## 범위 검토와 견적

- 업체 범위 제안은 고객이 만든 현재 `scope_version` 또는 고객 수정요청이 열린 현재 제안 version만 source로 사용한다. 제안은 새 불변 자식 version, 업체 approval, 원화 견적 snapshot, 포함·제외 작업과 사유를 한 transaction에 기록한다.
- 원화 견적은 `base_amount_krw + sum(adjustments.amount_krw) == total_amount_krw`를 만족해야 하고 adjustment label, 포함 작업과 제외 작업은 중복될 수 없다. 포함·제외 작업은 서로 겹칠 수 없다.
- 고객 수정요청은 업체 제안에 하나만 존재하고 후속 제안이 해결 관계를 기록한다. 과거 제안·요청·견적은 덮어쓰지 않는다.
- 고객 확인은 기존 `approve_scope_version`을 호출해 업체 approval과 합쳐 현재 version을 잠근다. 별도 event를 추가하지 않고 기존 `scope_locked.v1` envelope를 그대로 사용한다.
- 범위 preview는 `StoragePort.create_read_url`에 검증된 object key와 저장된 generation을 전달하지만 HTTP 응답에는 provider 내부값 대신 opaque 5분 HTTPS URL만 노출한다.

## 미디어 열람

- 변경요청 증거 열람 URL은 해당 작업의 고객·회사 관리자에게만 5분간 발급하며, 요청에 첨부되고 upload complete로 metadata가 고정된 `UPLOADED|READY` `change_evidence` 미디어와 저장된 generation만 허용한다.
- 변경 제안 화면은 같은 `UPLOADED|READY` 증거의 generation-pinned 5분 preview를 응답에 묶어 제공하고 object key·generation은 노출하지 않는다. 응답은 `Cache-Control: no-store`이며 고객 승인은 증거가 모두 `READY`일 때만 처리한다.

## 현장 이슈와 변경 제안

- 현장기사는 현재 잠긴 범위를 `base_scope_version_id`로 지정하고 자신이 업로드 완료한 `UPLOADED|READY` `change_evidence`를 하나 이상 첨부해 무가격 `field_issue`를 만든다. 업체는 generation이 고정된 `UPLOADED|READY` 증거로 변경 제안을 만들 수 있지만 고객 승인은 모든 증거가 `READY`일 때만 처리한다. 이슈 종류는 `out_of_scope|damage_risk|site_blocker`이며 금액·고객 결정은 입력할 수 없다.
- `field_issue`는 작업별 `client_reference`로 식별한다. 같은 reference와 정확히 같은 payload는 기존 결과를 반환하고 다른 payload, stale·잠기지 않은 범위 또는 다른 작업·촬영자의 증거는 거부한다.
- 업체만 이슈 하나를 기존 `change_request`에 연결된 `change_proposal_detail`로 전환한다. 제안의 기준 금액은 현재 확정 범위의 견적 또는 앞서 승인된 변경 제안 금액과 정확히 같아야 하며, 새 견적은 합계 불변식을 만족해야 한다.
- 제안 전송은 기존 `change_requested.v1` event를 재사용한다. 고객 승인 시 변경 결과 범위를 만들고 업체·고객 approval과 잠금을 한 transaction에 기록하며 기존 `scope_locked.v1`을 재사용한다. 현장기사나 업체는 고객 결정을 대신할 수 없다.
- 고객의 `approve|reject|request_clarification` 결정과 업체 설명은 terminal 또는 동일 설명의 정확 재전송만 멱등이다. 상충 결정·설명, 과거 기준 범위, 이슈 재사용과 승인 뒤 후속 변경은 거부한다.
- `field_issue`, `field_issue_evidence` 또는 `change_proposal_detail` 이력이 생긴 schema는 downgrade로 제거하지 않는다. rollback은 확장 schema를 유지한 application revision 전환으로 수행한다.

## 업체 범위 제안 실행계획

- 신규 `POST /move-jobs/{job_id}/scope-proposals`는 범위·원화 견적뿐 아니라 차량 수·차량 규격, 작업자 수, 예상 작업시간과 선택 메모를 `execution_plan`으로 반드시 받는다.
- 실행계획은 해당 불변 견적 version에 고정한다. 재전송 멱등성 비교에도 포함하며 어느 값이든 다르면 기존 제안을 재사용하지 않고 `409`다.
- `GET /scope-review`는 현재 합의 제안의 실행계획을 공동확인 카드에 반환한다. A-23 이전 legacy 제안과 업체 제안 전 고객 초안은 `null`로 명시한다.
- 실행계획이 저장된 schema는 downgrade로 제거하지 않는다. rollback은 확장 schema를 유지한 application revision 전환으로 수행한다.

## 미디어 검증 작업

- A는 `MEDIA_VALIDATION` background job의 생성·attempt·상태 전이를 소유하고, B는 A ORM 대신 version 1 task·work·result 계약과 후속 application command만 사용한다.
- `MediaValidationTaskV1` queue payload는 `background_job_id`, `job_type`, `attempt_count`, `schema_version`, `trace_id`만 포함한다. object key와 미디어 ID는 queue에 넣지 않는다.
- 후속 A command가 반환할 `MediaValidationWorkV1`은 object key, source generation, 기대 MIME type·크기를 한 attempt의 immutable 입력으로 고정한다. B는 반드시 그 generation의 객체만 읽는다.
- `MediaValidationResultV1`은 성공 시 관측 MIME type·크기와 64자 소문자 SHA-256을 모두 포함하고 오류를 포함하지 않는다. 실패 시 관측 metadata·hash 없이 정제된 `ProviderErrorKind` 하나만 포함한다. B는 A 소유 `MediaAssetStatus`를 결과로 결정하지 않는다.
- 후속 A command는 result의 generation과 관측 MIME type·크기를 work snapshot과 비교해야 한다. 성공 시 result의 source generation과 SHA-256을 asset에 저장하고 `READY`로 전이하며, 실패 시 `FAILED`로 전이하는 처리를 같은 transaction에서 수행해야 한다.
- result 멱등성 key는 `(background_job_id, attempt_count)`다. source generation이 다르거나 같은 attempt의 terminal 결과가 상충하면 후속 A command가 거부해야 한다.
- v1 제품은 검증을 통과한 원본 객체만 사용한다. 화면 preview는 저장된 generation을 고정한 짧은 `read_url`이며 thumbnail·poster·transcode 객체를 만들거나 DB에 파생 관계를 저장하지 않는다.
- 파생 파일은 v1 완료 조건이 아니다. 실제 소비 화면이 해상도·poster frame·codec 변환을 요구할 때 source asset·generation, derivative kind, 출력 MIME type, 크기·시간 정보, 상태, 보존·삭제 결합을 포함한 별도 versioned 계약으로 추가한다.
- 업로드 완료는 generation·MIME type·크기를 고정한 `PENDING` validation intent를 같은 transaction에 만든다. `start_media_validation`과 `complete_media_validation`은 기존 background-job lease·attempt를 재사용해 `UPLOADED|FAILED → PROCESSING → READY|FAILED`를 전이한다.
- B-05 handler는 A command로 attempt를 시작하고, StoragePort로 같은 generation의 metadata와 스트리밍 SHA-256을 확인한 뒤 정제된 result만 A command에 반환한다. object identity·MIME type·크기 불일치는 `INVALID_INPUT`, provider 오류는 해당 `ProviderErrorKind`로 실패 처리한다.
- media validation은 Cloud Tasks OIDC private worker가 version 1 task를 받아 이 handler를 실행한다. v1 handler는 원본 검증까지만 수행하며 미사용 파생 파일을 추정해 생성하지 않는다.

## 배차와 현장 체크인

- 회사 관리자만 `POST /move-jobs/{job_id}/dispatch/setup`으로 한 작업의 현재 잠긴 leaf 범위, 작업 예정 시각, 요구 차량 용량·인원·기술·자격, checklist와 차량·인력 후보 snapshot을 한 번 등록한다. 이 route는 업체 resource provider를 대체하는 master CRUD가 아니라 신뢰된 연동 경계다.
- snapshot에는 로그인하는 대표 현장기사 participant가 정확히 한 후보에 연결돼야 한다. 나머지 작업자는 작업 범위 안의 불변 배정 record이며 별도 access capability를 뜻하지 않는다.
- 회사 관리자의 `PUT /dispatch`는 setup ID, 차량 한 대, 중복 없는 정확한 인원과 lead worker를 받는다. 차량 가용성·용량, 작업자 가용성, 필수 기술·자격과 대표 현장기사 포함 여부를 다시 검증하고 한 번만 확정한다. 정확히 같은 command 재전송만 기존 결과를 반환한다.
- 확정은 `dispatch_plan`과 `dispatch_confirmed.v1` Outbox event를 같은 transaction에 기록한다. notification consumer는 event actor인 업체 관리자를 제외하고 해당 작업의 현장기사에게 `PENDING` in-app intent 하나를 만든다.
- 현장기사는 자신이 선택된 확정 배차와 현재 잠긴 leaf 범위에 대해서만 `GET /field-brief`를 조회한다. 신뢰할 연락처·채팅·지도 source가 없으므로 URI는 현재 `null`이며 server가 추정하지 않는다.
- `POST /check-ins`는 dispatch ID와 setup의 checklist key 전체를 중복 없이 받는다. 배정된 대표 현장기사와 작업 예정일 당일만 최초 기록하며 같은 key 집합의 재전송은 기존 체크인 시각을 반환한다.
- `dispatch_setup`, `dispatch_plan`, `field_check_in` 또는 `dispatch_confirmed.v1` 전달 이력이 생긴 schema는 downgrade로 제거하지 않는다. rollback은 확장 schema를 유지한 application revision 전환으로 수행한다.

## Event envelope

`DomainEvent`는 `event_id`, version이 포함된 `event_type`, `schema_version`, `aggregate_id`, `occurred_at`, 선택적 `actor_id`, `trace_id`, JSON `payload`로 구성한다. consumer는 `event_id`를 기준으로 중복 처리를 막는다.

초기 event는 다음과 같다.

- `capture_submitted.v1`
- `analysis_completed.v1`
- `analysis_failed.v1`
- `scope_locked.v1`
- `change_requested.v1`
- `dispatch_confirmed.v1`
- `completion_media_submitted.v1`
- `completion_submitted.v1`
- `completion_requested.v1`
- `completion_decided.v1`
- `media_deleted.v1`

현재 A 업무 command가 생성하는 event payload는 아래 key만 정확히 포함한다. 모든 ID는 canonical UUID 문자열이고 `content_hash`는 64자 소문자 16진수다. 증거 ID 배열은 비어 있지 않고 중복이 없다. 주소·자유서술·역할 링크·signed URL은 event에 넣지 않는다.

- `scope_locked.v1`: 문자열 `scope_version_id`, 문자열 `content_hash`
- `capture_submitted.v1`: 문자열 `capture_session_id`, 문자열 `analysis_run_id`, 비어 있지 않고 중복 없는 문자열 배열 `inventory_media_asset_ids`
- `analysis_completed.v1`: 문자열 `capture_session_id`, 문자열 `analysis_run_id`, 문자열 `scope_version_id`
- `analysis_failed.v1`: 문자열 `capture_session_id`, 문자열 `analysis_run_id`, provider-neutral `error_kind`, boolean `retryable`
- `change_requested.v1`: 문자열 `change_request_id`, 문자열 `base_scope_version_id`, 문자열 배열 `evidence_media_asset_ids`
- `dispatch_confirmed.v1`: 문자열 `dispatch_id`, 문자열 `scope_version_id`, 문자열 `field_worker_participant_id`
- `completion_media_submitted.v1`: 문자열 `capture_session_id`, 문자열 `media_asset_id`, 문자열 `room_zone_id`
- `completion_submitted.v1`: 문자열 `completion_submission_id`, 문자열 `scope_version_id`, 문자열 `field_worker_participant_id`
- `completion_requested.v1`: 문자열 `completion_request_id`, 문자열 `completion_submission_id`, 문자열 `customer_participant_id`
- `completion_decided.v1`: 문자열 `completion_request_id`, 문자열 `completion_submission_id`, `confirm|report_issue` `decision`, 선택적 문자열 `problem_report_id`
- `media_deleted.v1`: 문자열 `background_job_id`, 문자열 `media_asset_id`

## AI 분석 실행

- `GET /api/v1/move-jobs/{job_id}/media-consent-policy`는 현재 동의문 버전, AI 초안·조건 기록·현장 변경·완료 증빙 목적과 작업 완료 뒤 보관 일수를 반환한다. `SEQRET_MEDIA_RETENTION_DAYS`가 없으면 `503`으로 fail-closed한다.
- `POST /api/v1/move-jobs/{job_id}/capture-sessions`는 현재 `consent_policy_version`과 명시적 `privacy_notice_acknowledged: true`를 요구한다. server는 정책 버전, 목적, 보관기간과 동의 시각을 세션별 불변 snapshot으로 저장한다. 기존 세션은 동의한 것으로 간주하지 않고 `privacy_notice_acknowledged: false`와 빈 목적 목록으로 표시하며 새 upload·분석 제출을 거부한다.
- `GET /api/v1/move-jobs/{job_id}/capture-sessions`는 호출자가 직접 만든 촬영 세션만 최신순으로 반환한다. 각 세션은 생성 정보, 동의 snapshot, 생성순 미디어의 public metadata·처리 상태와 선택적 provider-neutral 분석 상태를 포함한다. object key, generation, signed URL, queue lease와 provider task ID는 반환하지 않으며, 세션이 없으면 `200 []`다.
- 촬영 세션 소유자는 `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/submit`으로 분석을 한 번만 제출한다. `inventory` 미디어가 하나 이상이고 전부 `READY`일 때만 `202`를 반환하며, 제출 뒤 같은 촬영 세션의 새 upload·미완료 upload 확정은 `409`로 막는다. 같은 제출 replay는 동일한 `analysis_run_id`와 상태를 반환한다.
- `GET /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/analysis`는 촬영 소유자에게 `pending|dispatching|queued|running|completed|failed`, 선택적 `scope_version_id`, provider-neutral `failure_code`·`retryable`만 반환한다. object key, provider task 원문과 provider 오류 원문은 노출하지 않는다.
- A의 `capture_analysis_dispatch`는 제출, Cloud Tasks enqueue lease·backoff, worker 실행, 결과 import와 terminal 상태를 소유한다. due row는 `FOR UPDATE SKIP LOCKED`로 선점하고 `analysis:{analysis_run_id}:attempt:1` key로 enqueue한다. enqueue 실패는 정제된 오류 코드만 저장한 뒤 최대 300초 지수 backoff로 재시도한다.
- `ai_analysis_run`, `detection`, `analysis_location_condition_suggestion`은 사람이 검토할 파생 초안만 저장한다. v1 detection은 기존 필드만 유지하고 v2 detection은 이름·수량·단위·작업 메모를 함께 보존한다. 위치 조건 제안은 A의 location ORM을 참조하지 않고 계약 ID·종류와 구조화 조건·검수 필드·출처만 저장한다. B는 `scope_version`을 생성·수정·잠금하지 않고, 확정 범위 반영은 A의 `ImportAnalysisDraft` command만 수행한다.
- Vertex adapter는 각 품목과 위치 조건의 media 출처를 provider 출력 단계에서 검증한다. v1 단일 입력은 provider index 표현과 무관하게 유일한 source로 정규화하는 호환 동작을 유지한다. v2는 단일 입력도 명시적이고 유효한 index를 요구하고, 위치 제안의 모든 source가 같은 server-owned 비주소 location context인지 확인한다. 비었거나 중복·범위 밖·서로 다른 위치가 섞인 출처는 `source_map / invalid_input / retryable=false`로 거부한다.
- Vertex v2 prompt에는 주소·사용자·UUID 대신 입력 순번, 출·도착 종류와 익명 위치·방 group만 전달한다. provider는 최종 가격이나 ID를 결정하지 않으며 `unknown`과 `review_required_fields`로 불확실성을 보존한다.
- `analysis_run_id`가 실행 멱등성 key다. start·complete·fail은 해당 run을 잠그며, 같은 start·결과·오류 replay는 no-op이고 capture session이나 terminal 결과가 다르면 `AnalysisRunConflictError`다.
- private worker는 먼저 A의 `start_capture_analysis` command를 호출하고, B handler 완료 후 `AnalysisResult`만 A의 `complete_capture_analysis`에 전달한다. A는 기존 `import_analysis_draft`를 통해 편집 가능하고 잠기지 않은 `scope_version`을 만들며 `analysis_completed.v1`을 같은 transaction에 기록한다. READY 입력이 사라졌거나 결과를 안전하게 가져올 수 없으면 `analysis_failed.v1`을 기록하고 수동 작업 경로를 유지한다.
- B-06은 실패 run을 새 attempt로 여는 명시적 command를 제공한다. INT-06은 저장된 status·failure snapshot과 row-locked retry 준비 command로 FAILED 저장 직후 재전달도 원래 error kind·retryability를 복원한다. retryable attempt는 worker 503과 Cloud Tasks 재전달로 이어지고, 동시 재전달·범위 import는 PostgreSQL 잠금과 dedup으로 한 번만 반영된다.
- v2 analysis result나 위치 조건 제안이 생성된 뒤에는 B-08 schema downgrade를 금지한다. capture analysis dispatch나 analysis run이 생성된 더 오래된 schema도 자체 guard를 유지한다. 장애 복구는 확장 schema를 유지한 채 이전 application revision으로 되돌린다.

## 완료와 감사 이력

- 대표 현장기사만 현재 확정 배차의 체크인 뒤 `completion_submission`을 만든다. 현재 잠긴 leaf 범위, setup의 완료 checklist 전체, 배정 작업자 전원의 중복 없는 근무 구간, 작업 종료·현장 고객 확인 시각과 선택적 completion 미디어를 검증한다. 미디어가 있으면 해당 기사가 upload complete를 마친 `UPLOADED|READY` 객체와 비어 있지 않은 generation만 받으며 고객의 최종 확인은 모든 첨부가 `READY`일 때만 처리한다.
- `client_reference`와 정확한 제출 payload 재전송은 최초 불변 제출을 반환한다. 상충 재전송, 살아 있는 고객 요청 또는 이미 확인된 제출 뒤의 새 제출은 거부한다. 고객 문제 신고·요청 만료·철회 뒤에는 정정 제출을 허용한다.
- 업체는 최신 제출에 대해 7일 유효한 `completion_request`를 만들거나 살아 있는 요청을 철회한다. 같은 `client_reference`와 정확한 payload만 멱등이며, 이전 제출·중복 활성 요청·이미 문제 신고된 같은 제출의 재요청은 거부한다.
- 고객은 최신 살아 있는 요청 하나만 `confirm|report_issue`로 결정한다. 문제 신고는 `missing_work|damage|amount|other`와 설명을 별도 append-only row에 저장하며 원인·책임을 자동 판정하지 않는다. 확인은 고객·업체 `completion_confirmation`, 작업 `COMPLETED`, 감사·Outbox와 선택적 미디어 보존 intent를 한 transaction에서 기록한다.
- `completion-summary`는 업체와 완료 요청을 받은 고객에게만 현재 제출·요청, 최종 견적·변경, 항목별 확인값이 포함된 체크리스트·근무·선택적 `UPLOADED|READY` generation-pinned preview, 문서 준비 상태와 보존기한을 제공한다. 고객은 요청 전 요약을 읽을 수 없고 업체만 문서 ZIP을 내려받는다.
- 문서 archive는 준비된 견적과 완료 제출을 바탕으로 견적서, 변경 승인 기록, 작업 완료 기록, 완료 확인 기록 PDF와 schema v1 manifest를 결정적으로 생성한다. 문서 실패는 완료·결정 DB 사실을 되돌리지 않으며 필수 자료가 없으면 빈 ZIP 대신 `409`다.
- 유효한 access link의 `last_used_at`이 처음 기록되는 인증 transaction은 `PARTICIPANT_CONNECTED` 감사 event를 정확히 한 번 함께 기록한다. 이후 같은 link 사용은 시각만 갱신한다.
- `PARTICIPANT_CONNECTED`의 actor는 해당 참여자이며 payload는 문자열 `access_link_id`, 문자열 `participant_id`, 역할 `role`만 포함한다. bearer secret, token hash와 request 식별자는 넣지 않는다.
- `audit_event`는 DB에서 UPDATE·DELETE를 거부하고 PostgreSQL에서는 TRUNCATE도 거부한다. application에는 수정·삭제 command를 두지 않는다.
- 감사 event, 완료 확인·제출·요청·문제 또는 사용자 지정 완료 checklist가 하나라도 생성된 뒤에는 이력을 지우는 schema downgrade를 금지한다. 장애 복구는 schema를 유지한 채 이전 application revision으로 되돌린다.

## 미디어 보존 작업

- 보존기간은 `SEQRET_MEDIA_RETENTION_DAYS`로 운영 환경이 명시한다. 값이 없으면 완료 확인과 삭제 작업 생성 API는 fail-closed로 동작한다.
- 완료 미디어는 선택 사항이지만 제공된 증거는 제출 시점부터 `READY` 상태와 generation이 필요하다. 최종 고객 확인은 모든 제출 증거의 object key·generation을 고정하고 결정 시각 + `SEQRET_MEDIA_RETENTION_DAYS`의 `PENDING` 삭제 intent를 같은 transaction에 만든다. 미디어가 없으면 삭제 intent는 만들지 않는다.
- 기존 완료 작업을 보완하는 수동 API는 보존기간이 지난 `UPLOADED`, `READY`, `FAILED` 미디어 중 generation이 확인된 객체만 즉시 삭제 대상으로 고정한다. 실행 중이거나 업로드·generation이 확인되지 않은 미디어는 대상에 넣지 않는다.
- `MediaDeletionTaskV1` queue payload는 `background_job_id`, `job_type`, `attempt_count`, `schema_version`, `trace_id`만 포함한다. object key와 generation은 B handler가 `start_media_deletion` application query로 얻는다.
- enqueue는 DB intent commit 뒤 lease dispatcher가 수행하고 `background-job:{id}:attempt:{n}` key로 중복 생성을 막는다.
- B handler는 `app.modules.background_job.service.start_media_deletion(session, task)`를 먼저 호출한다. 현재 attempt·trace가 아니면 `BackgroundJobNotFoundError`, 실행할 수 없는 상태면 `BackgroundJobConflictError`, 이미 같은 attempt가 실행 중이면 같은 immutable work를 반환하고, terminal이면 `None`을 반환한다. work의 object key·generation은 provider 호출과 로그 밖으로 복제하지 않는다.
- B handler의 `StoragePort.delete_object` key는 attempt와 무관한 `media-delete:{background_job_id}`다. snapshot generation이 이미 없으면 성공으로 처리하고, 다른 generation의 객체는 삭제하지 않는다. B-07 adapter 계약 테스트는 서로 다른 두 attempt가 같은 삭제 효과로 끝나는지 검증한다.
- 실행 lease 기본값은 15분이다. lease가 지난 `RUNNING` 작업만 manager 재실행 API가 새 attempt로 돌릴 수 있으며, 살아 있는 실행과 중복시키는 조기 재실행은 `409`로 막는다.
- B는 실제 삭제를 시도한 `RUNNING` attempt의 `MediaDeletionResultV1`을 `complete_media_deletion` command에 반환한다. 같은 결과 replay는 no-op이고 stale·상충 result는 `BackgroundJobConflictError`다. 성공 시 A가 미디어 상태와 `media_deleted.v1` Outbox를 한 transaction에서 반영한다. 작업 생성자·시도 횟수·마지막 오류·terminal 상태는 `background_job`에 보존한다.
- 물리 객체 목록이 필요한 고아 탐지는 B의 listing 계약이 생기기 전까지 추정하지 않는다. Outbox 정합성 재시도는 기존 relay를 사용한다.
- background job row가 생성된 뒤에는 운영 이력을 지우는 schema downgrade를 금지한다. 장애 복구는 확장 schema를 유지한 채 이전 application revision으로 되돌린다. 기존 감사 enum을 확장하지 않아 직전 application revision도 기존 감사 이력을 계속 읽을 수 있다.
- 실제 queue adapter와 private handler runtime은 B-02/B-07 소유다. 매분 scheduled relay가 `dispatch_background_jobs_once`를 호출하고 Cloud Tasks의 OIDC identity만 internal private worker를 호출한다. worker는 validation·retention task를 versioned discriminator로 검증한 뒤 해당 B handler를 실행한다.

## Outbox와 소비 멱등성

- 업무 상태와 event envelope는 같은 PostgreSQL transaction에서 commit한다.
- relay는 due row를 `FOR UPDATE SKIP LOCKED`로 lease하고, 한 batch의 provider 발행을 동시에 시작해 아직 시도하지 않은 row의 lease 만료를 방지한다.
- Pub/Sub 메시지 body는 `DomainEvent` JSON이며 `event_id`, `event_type`, `schema_version`, `idempotency_key`를 attribute로도 전달한다.
- publish 완료 후 DB 반영이 실패하면 같은 `event_id`가 다시 발행될 수 있으므로 전달 의미는 at-least-once이다.
- consumer는 `(consumer_name, event_id)` receipt를 먼저 기록하고, 같은 event의 중복 효과를 만들지 않는다.
- Outbox 실패는 정제된 오류 분류와 시도 횟수만 저장하고 1초부터 최대 300초까지 지수 backoff로 다시 시도한다. payload나 provider 오류 원문은 운영 오류 필드에 복제하지 않는다.
- `outbox_event`는 현재 지원하는 `schema_version = 1`과 JSON object payload만 저장한다. event별 정확한 payload shape 검증은 `DomainEvent` 계약이 담당한다.
- Outbox·알림·소비 receipt가 하나라도 생성된 뒤에는 운영 이력을 지우는 schema downgrade를 금지한다. 장애 복구는 schema를 유지한 채 이전 application revision으로 되돌린다.

## 외부 알림 전달

- notification consumer는 기존 in-app intent를 항상 유지한다. event 생성 시점에 recipient의 active workspace membership과 명시적으로 동의한 연락처가 있으면 `email|sms|kakao` delivery를 채널별로 추가한다. 연락처 등록은 과거 event를 소급 발송하지 않는다.
- 연락처 원문은 전달을 위해 DB에 저장하지만 API는 마스킹된 값만 반환한다. 이메일은 일반 이메일 형식, SMS·카카오는 한국 E.164 `+82...`만 허용하고 log·trace·error에는 원문을 기록하지 않는다.
- 연락처를 교체·비활성·삭제하면 이전 destination의 아직 `PENDING`인 delivery를 `FAILED/consent_revoked`로 전이한다. 이미 `SENT`인 기록은 감사 목적상 보존한다.
- relay는 provider 호출 직전에 한 건씩 lease하고 최대 10건을 동시에 처리한다. provider가 재시도 가능 오류를 반환하면 지수 backoff로 최대 5회까지 시도하고, 영구 오류 또는 마지막 시도는 정제된 error code와 함께 `FAILED`로 끝낸다.
- 외부 발송은 at-least-once다. 알림톡은 NHN의 10분 idempotency key 계약을 사용하지만 Email·SMS grouping key는 중복 억제를 보장하지 않으므로 provider가 수신 후 응답을 잃은 timeout 재시도에서 중복이 생길 수 있다. 운영자는 provider request ID와 notification ID로 확인한다.
- NHN Cloud 설정은 전체가 준비된 경우에만 `notification_delivery_enabled=true`를 허용한다. Email·SMS·알림톡 app/secret·등록 발신자, 알림톡 발신 프로필과 승인 템플릿, frontend origin 중 하나라도 없으면 시작을 거부한다.

## 오류와 호환성

공개 API의 `422` 요청 검증 오류는 OpenAPI에 정의된 `type`, `loc`, `msg`만 반환한다. Pydantic의 원문 `input`, 내부 `ctx`, 문서 `url`은 응답에 포함하지 않는다.

공통 fake는 누락된 object key 같은 결정적 오류를 `ProviderError`로 재현한다. `not_found`, `conflict`, `invalid_input`, `unavailable`, `deadline_exceeded`, `permission_denied` 분류와 `retryable` 여부는 provider SDK 타입을 application 계층에 노출하지 않는다. 기존 version 1 필드를 제거하거나 의미를 바꾸지 않으며 optional 필드 추가 또는 새 schema version으로 확장한다.
