# 공통 계약

이 문서는 트랙 A와 B가 공유하는 provider-independent 계약의 version 1을 정의한다. 계약 구현은 `app/contracts`에 있고, 두 트랙은 상대 트랙의 ORM model이나 repository 대신 이 계약만 사용한다.

## 공통 원칙

- 모든 외부 경계 모델은 알 수 없는 필드를 거부하고 생성 후 변경할 수 없다.
- 식별자는 UUID 기반 nominal type이며, 시각은 timezone-aware 값만 허용한다.
- `ActorContext`는 인증 계층이 검증한 작업·참여자·역할 capability와 권한 경계를 application command에 전달한다. bearer 링크는 개인 신원을 증명하지 않으며 원문 역할 토큰은 포함하지 않는다.
- `DomainEvent.schema_version`의 최초 값은 `1`이다. 기존 event payload를 깨는 변경은 event 이름과 schema version을 새로 추가한다.
- signed URL, 역할 토큰, 주소 원문과 원본 미디어는 모델 repr이나 로그에 남기지 않는다.

## Port 소유권과 동작

| Port | adapter 소유자 | 입력·출력 | 멱등성 | timeout | 오류 계약 |
| --- | --- | --- | --- | --- | --- |
| `StoragePort` (`ObjectStoragePort` 호환 별칭) | B | object key와 제약 → signed upload target, read URL, metadata, generation-pinned SHA-256 또는 삭제 완료 | 삭제는 `idempotency_key`로 중복 효과를 막는다 | 모든 provider 호출에 초 단위 명시 | provider 오류를 adapter 예외로 매핑하며 A ORM을 갱신하지 않는다 |
| `TaskQueuePort` | B | queue, handler, JSON payload → provider task ID | 같은 key는 한 task만 반환한다 | enqueue 호출에 초 단위 명시 | 재시도 가능 여부를 provider 외부 타입으로 노출하지 않는다 |
| `AIProviderPort` | B | `AnalysisRequest`의 분석·촬영·미디어 ID, object key, model/prompt version → `AnalysisResult` | 같은 key는 같은 입력에 같은 분석 결과를 반환한다 | 분석 호출에 초 단위 명시 | 결과는 초안이며 `scope_version`을 생성하거나 잠그지 않는다 |
| `EventBusPort` | A | `DomainEvent` → 발행 완료 | event ID 기반 key로 중복 발행 효과를 막는다 | 발행 호출에 초 단위 명시 | Outbox 상태와 retry 정책은 A가 관리한다 |
| `CachePort` | A | namespace가 포함된 key와 fixed-window 길이 → 원자적으로 증가한 count | 같은 window의 증가가 기존 TTL을 연장하지 않는다 | cache 호출에 초 단위 명시 | Redis 오류는 adapter 예외로 매핑하며 application은 DB 원본 제한을 계속 적용한다 |

로컬 fake는 실제 adapter와 같은 Protocol을 만족하고 멱등 동작을 contract test로 검증한다.

- `StoragePort.create_upload_url`은 객체가 없을 때만 생성하는 immutable `StorageUploadTarget`을 반환한다. 공용 모델은 provider별 URL과 header를 해석하거나 정규화하지 않는 opaque bag이며, API는 그 값을 `upload_url`과 `upload_headers`로 정확히 전달한다. 각 adapter가 provider별 create-only 조건을 서명하고 서명에 필요한 모든 header를 target에 넣는다. 현재 GCS adapter는 요청 `Content-Type`과 `x-goog-if-generation-match: 0`을 함께 서명·반환해야 한다.
- `StoragePort.create_read_url`은 DB에 검증·저장된 object generation을 필수로 받고, adapter는 그 generation을 signed URL에 고정한다. generation이 없는 미디어는 열람 URL 발급을 거부한다.
- `StoragePort.calculate_sha256`은 지정한 generation만 스트리밍해 소문자 SHA-256을 반환한다. 원본 전체를 메모리에 적재하거나 최신 generation으로 대체하지 않는다.
- `StoragePort.delete_object`의 generation은 1~255자의 필수 snapshot이다. snapshot generation이 이미 없으면 멱등 성공하고, 같은 key의 다른 generation 객체는 보존한다.
- 업로드 완료 command는 metadata의 object key, MIME type, 크기와 generation을 모두 검증하며, signed URL과 필수 header는 cache하거나 문자열을 정규화하지 않고 원문 그대로 전달한다.
- `PENDING_UPLOAD` 미디어는 실제 크기·hash·generation·업로드 시각을 갖지 않는다. 그 밖의 상태는 실제 크기, 비어 있지 않은 generation과 업로드 시각을 반드시 보존한다.
- `AnalysisRequest.source_media_asset_ids[n]`, `object_keys[n]`, `content_types[n]`은 같은 미디어를 가리킨다. 세 배열은 길이가 같고 ID와 object key 배열 안에서 중복이 없어야 한다. `content_types`는 A가 validation을 끝낸 `image/jpeg`, `image/png`, `video/mp4` 중 하나이며 B adapter는 확장자가 없는 object key에서 MIME type을 추정하지 않는다.
- merge 순서는 이 계약과 fake → B의 Storage adapter rebase → 실제 provider 통합 검증이다. upload 반환형과 delete generation은 호환성을 깨는 계약 변경이므로 기존 B adapter를 먼저 병합하지 않는다.

## 미디어 열람

- 변경요청 증거 열람 URL은 해당 작업의 고객·회사 관리자에게만 5분간 발급하며, 요청에 첨부된 `READY` 상태의 `change_evidence` 미디어와 저장된 generation만 허용한다.

## 미디어 검증 작업

- A는 `MEDIA_VALIDATION` background job의 생성·attempt·상태 전이를 소유하고, B는 A ORM 대신 version 1 task·work·result 계약과 후속 application command만 사용한다.
- `MediaValidationTaskV1` queue payload는 `background_job_id`, `job_type`, `attempt_count`, `schema_version`, `trace_id`만 포함한다. object key와 미디어 ID는 queue에 넣지 않는다.
- 후속 A command가 반환할 `MediaValidationWorkV1`은 object key, source generation, 기대 MIME type·크기를 한 attempt의 immutable 입력으로 고정한다. B는 반드시 그 generation의 객체만 읽는다.
- `MediaValidationResultV1`은 성공 시 관측 MIME type·크기와 64자 소문자 SHA-256을 모두 포함하고 오류를 포함하지 않는다. 실패 시 관측 metadata·hash 없이 정제된 `ProviderErrorKind` 하나만 포함한다. B는 A 소유 `MediaAssetStatus`를 결과로 결정하지 않는다.
- 후속 A command는 result의 generation과 관측 MIME type·크기를 work snapshot과 비교해야 한다. 성공 시 result의 source generation과 SHA-256을 asset에 저장하고 `READY`로 전이하며, 실패 시 `FAILED`로 전이하는 처리를 같은 transaction에서 수행해야 한다.
- result 멱등성 key는 `(background_job_id, attempt_count)`다. source generation이 다르거나 같은 attempt의 terminal 결과가 상충하면 후속 A command가 거부해야 한다.
- 이 계약은 파생 파일 형식·저장 모델을 추정하지 않는다. 해당 제품 정책이 확정되면 별도 versioned 계약으로 추가한다.
- 업로드 완료는 generation·MIME type·크기를 고정한 `PENDING` validation intent를 같은 transaction에 만든다. `start_media_validation`과 `complete_media_validation`은 기존 background-job lease·attempt를 재사용해 `UPLOADED|FAILED → PROCESSING → READY|FAILED`를 전이한다.
- B-05 handler는 A command로 attempt를 시작하고, StoragePort로 같은 generation의 metadata와 스트리밍 SHA-256을 확인한 뒤 정제된 result만 A command에 반환한다. object identity·MIME type·크기 불일치는 `INVALID_INPUT`, provider 오류는 해당 `ProviderErrorKind`로 실패 처리한다.
- 파생 파일 형식·저장 정책은 아직 결정되지 않았다. media validation은 Cloud Tasks OIDC private worker가 version 1 task를 받아 이 handler를 실행한다.

## Event envelope

`DomainEvent`는 `event_id`, version이 포함된 `event_type`, `schema_version`, `aggregate_id`, `occurred_at`, 선택적 `actor_id`, `trace_id`, JSON `payload`로 구성한다. consumer는 `event_id`를 기준으로 중복 처리를 막는다.

초기 event는 다음과 같다.

- `capture_submitted.v1`
- `analysis_completed.v1`
- `analysis_failed.v1`
- `scope_locked.v1`
- `change_requested.v1`
- `completion_media_submitted.v1`
- `media_deleted.v1`

현재 A 업무 command가 생성하는 event payload는 아래 key만 정확히 포함한다. 모든 ID는 canonical UUID 문자열이고 `content_hash`는 64자 소문자 16진수다. 증거 ID 배열은 비어 있지 않고 중복이 없다. 주소·자유서술·역할 링크·signed URL은 event에 넣지 않는다.

- `scope_locked.v1`: 문자열 `scope_version_id`, 문자열 `content_hash`
- `capture_submitted.v1`: 문자열 `capture_session_id`, 문자열 `analysis_run_id`, 비어 있지 않고 중복 없는 문자열 배열 `inventory_media_asset_ids`
- `analysis_completed.v1`: 문자열 `capture_session_id`, 문자열 `analysis_run_id`, 문자열 `scope_version_id`
- `analysis_failed.v1`: 문자열 `capture_session_id`, 문자열 `analysis_run_id`, provider-neutral `error_kind`, boolean `retryable`
- `change_requested.v1`: 문자열 `change_request_id`, 문자열 `base_scope_version_id`, 문자열 배열 `evidence_media_asset_ids`
- `completion_media_submitted.v1`: 문자열 `capture_session_id`, 문자열 `media_asset_id`, 문자열 `room_zone_id`
- `media_deleted.v1`: 문자열 `background_job_id`, 문자열 `media_asset_id`

## AI 분석 실행

- `ai_analysis_run`과 `detection`은 사람이 검토할 파생 초안만 저장한다. B는 `scope_version`을 생성·수정·잠금하지 않고, 확정 범위 반영은 A의 `ImportAnalysisDraft` command만 수행한다.
- `analysis_run_id`가 실행 멱등성 key다. start·complete·fail은 해당 run을 잠그며, 같은 start·결과·오류 replay는 no-op이고 capture session이나 terminal 결과가 다르면 `AnalysisRunConflictError`다.
- B-03의 기존 run start는 terminal 상태에서도 no-op이다. 새 attempt를 여는 명시적 retry와 stale attempt 차단 token은 worker retry 정책을 소유하는 B-06에서 추가한다.
- analysis run이 생성된 뒤에는 파생 이력을 지우는 schema downgrade를 금지한다. 장애 복구는 확장 schema를 유지한 채 이전 application revision으로 되돌린다.

## 완료와 감사 이력

- 유효한 access link의 `last_used_at`이 처음 기록되는 인증 transaction은 `PARTICIPANT_CONNECTED` 감사 event를 정확히 한 번 함께 기록한다. 이후 같은 link 사용은 시각만 갱신한다.
- `PARTICIPANT_CONNECTED`의 actor는 해당 참여자이며 payload는 문자열 `access_link_id`, 문자열 `participant_id`, 역할 `role`만 포함한다. bearer secret, token hash와 request 식별자는 넣지 않는다.
- `audit_event`는 DB에서 UPDATE·DELETE를 거부하고 PostgreSQL에서는 TRUNCATE도 거부한다. application에는 수정·삭제 command를 두지 않는다.
- 감사 event, 완료 확인 또는 완료 증거가 하나라도 생성된 뒤에는 이력을 지우는 schema downgrade를 금지한다. 장애 복구는 schema를 유지한 채 이전 application revision으로 되돌린다.

## 미디어 보존 작업

- 보존기간은 `SEQRET_MEDIA_RETENTION_DAYS`로 운영 환경이 명시한다. 값이 없으면 완료 확인과 삭제 작업 생성 API는 fail-closed로 동작한다.
- 완료 증거는 첫 확인부터 generation이 필요하다. 최종 양측 확인은 모든 완료 증거의 object key·generation을 고정하고 `completed_at + SEQRET_MEDIA_RETENTION_DAYS` 시각의 `PENDING` 삭제 intent를 같은 transaction에 만든다.
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

## 오류와 호환성

공통 fake는 누락된 object key 같은 결정적 오류를 `ProviderError`로 재현한다. `not_found`, `conflict`, `invalid_input`, `unavailable`, `deadline_exceeded`, `permission_denied` 분류와 `retryable` 여부는 provider SDK 타입을 application 계층에 노출하지 않는다. 기존 version 1 필드를 제거하거나 의미를 바꾸지 않으며 optional 필드 추가 또는 새 schema version으로 확장한다.
