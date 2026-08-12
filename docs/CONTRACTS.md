# 공통 계약

이 문서는 트랙 A와 B가 공유하는 provider-independent 계약의 version 1을 정의한다. 계약 구현은 `app/contracts`에 있고, 두 트랙은 상대 트랙의 ORM model이나 repository 대신 이 계약만 사용한다.

## 공통 원칙

- 모든 외부 경계 모델은 알 수 없는 필드를 거부하고 생성 후 변경할 수 없다.
- 식별자는 UUID 기반 nominal type이며, 시각은 timezone-aware 값만 허용한다.
- `ActorContext`는 인증 계층이 검증한 신원과 한 작업의 권한 경계를 application command에 전달한다. 원문 역할 토큰은 포함하지 않는다.
- `ErrorResponse.schema_version`과 `DomainEvent.schema_version`의 최초 값은 `1`이다. 기존 event payload를 깨는 변경은 event 이름과 schema version을 새로 추가한다.
- signed URL, 역할 토큰, 주소 원문과 원본 미디어는 모델 repr이나 로그에 남기지 않는다.

## Port 소유권과 동작

| Port | adapter 소유자 | 입력·출력 | 멱등성 | timeout | 오류 계약 |
| --- | --- | --- | --- | --- | --- |
| `StoragePort` (`ObjectStoragePort` 호환 별칭) | B | object key와 제약 → signed URL, metadata 또는 삭제 완료 | 삭제는 `idempotency_key`로 중복 효과를 막는다 | 모든 provider 호출에 초 단위 명시 | provider 오류를 adapter 예외로 매핑하며 A ORM을 갱신하지 않는다 |
| `TaskQueuePort` | B | queue, handler, JSON payload → provider task ID | 같은 key는 한 task만 반환한다 | enqueue 호출에 초 단위 명시 | 재시도 가능 여부를 provider 외부 타입으로 노출하지 않는다 |
| `AIProviderPort` | B | `AnalysisRequest`의 분석·촬영·미디어 ID, object key, model/prompt version → `AnalysisResult` | 같은 key는 같은 입력에 같은 분석 결과를 반환한다 | 분석 호출에 초 단위 명시 | 결과는 초안이며 `scope_version`을 생성하거나 잠그지 않는다 |
| `EventBusPort` | A | `DomainEvent` → 발행 완료 | event ID 기반 key로 중복 발행 효과를 막는다 | 발행 호출에 초 단위 명시 | Outbox 상태와 retry 정책은 A가 관리한다 |
| `CachePort` | A | namespace가 포함된 key와 fixed-window 길이 → 원자적으로 증가한 count | 같은 window의 증가가 기존 TTL을 연장하지 않는다 | cache 호출에 초 단위 명시 | Redis 오류는 adapter 예외로 매핑하며 DB fallback 선택은 application 정책이 관리한다 |

로컬 fake는 실제 adapter와 같은 Protocol을 만족하고 멱등 동작을 contract test로 검증한다.

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

현재 A 업무 command가 생성하는 event payload는 다음 최소 식별자만 포함한다. 주소·자유서술·역할 링크·signed URL은 event에 넣지 않는다.

- `scope_locked.v1`: `scope_version_id`, `content_hash`
- `change_requested.v1`: `change_request_id`, `base_scope_version_id`, `evidence_media_asset_ids`
- `completion_media_submitted.v1`: `capture_session_id`, `media_asset_id`, `room_zone_id`
- `media_deleted.v1`: `background_job_id`, `media_asset_id`

## 미디어 보존 작업

- 보존기간은 `SEQRET_MEDIA_RETENTION_DAYS`로 운영 환경이 명시한다. 값이 없으면 삭제 작업 생성 API는 fail-closed로 동작한다.
- 완료된 작업의 보존기간이 지난 `UPLOADED`, `READY`, `FAILED` 미디어 중 generation이 확인된 객체만 삭제 대상으로 고정한다. 실행 중이거나 업로드·generation이 확인되지 않은 미디어는 대상에 넣지 않는다.
- `MediaDeletionTaskV1` queue payload는 `background_job_id`, `job_type`, `attempt_count`, `schema_version`, `trace_id`만 포함한다. object key와 generation은 B handler가 `start_media_deletion` application query로 얻는다.
- enqueue는 DB intent commit 뒤 lease dispatcher가 수행하고 `background-job:{id}:attempt:{n}` key로 중복 생성을 막는다.
- B handler는 `app.modules.background_job.service.start_media_deletion(session, task)`를 먼저 호출한다. 현재 attempt·trace가 아니면 `BackgroundJobNotFoundError`, 실행할 수 없는 상태면 `BackgroundJobConflictError`, 이미 같은 attempt가 실행 중이면 같은 immutable work를 반환하고, terminal이면 `None`을 반환한다. work의 object key·generation은 provider 호출과 로그 밖으로 복제하지 않는다.
- B handler의 `StoragePort.delete_object` key는 attempt와 무관한 `media-delete:{background_job_id}`다. snapshot generation이 이미 없으면 성공으로 처리하고, 다른 generation의 객체는 삭제하지 않는다. B-07 adapter 계약 테스트는 서로 다른 두 attempt가 같은 삭제 효과로 끝나는지 검증한다.
- 실행 lease 기본값은 15분이다. lease가 지난 `RUNNING` 작업만 manager 재실행 API가 새 attempt로 돌릴 수 있으며, 살아 있는 실행과 중복시키는 조기 재실행은 `409`로 막는다.
- B는 실제 삭제를 시도한 `RUNNING` attempt의 `MediaDeletionResultV1`을 `complete_media_deletion` command에 반환한다. 같은 결과 replay는 no-op이고 stale·상충 result는 `BackgroundJobConflictError`다. 성공 시 A가 미디어 상태와 `media_deleted.v1` Outbox를 한 transaction에서 반영한다. 작업 생성자·시도 횟수·마지막 오류·terminal 상태는 `background_job`에 보존한다.
- 물리 객체 목록이 필요한 고아 탐지는 B의 listing 계약이 생기기 전까지 추정하지 않는다. Outbox 정합성 재시도는 기존 relay를 사용한다.
- background job row가 생성된 뒤에는 운영 이력을 지우는 schema downgrade를 금지한다. 장애 복구는 확장 schema를 유지한 채 이전 application revision으로 되돌린다. 기존 감사 enum을 확장하지 않아 직전 application revision도 기존 감사 이력을 계속 읽을 수 있다.
- 실제 queue adapter와 private handler runtime은 B-02/B-07 소유다. A가 제공하는 `dispatch_background_jobs_once` 호출은 그 adapter가 병합될 때 scheduled runtime에 연결하며, 그 전에는 durable `PENDING` intent를 잃지 않는다.

## Outbox와 소비 멱등성

- 업무 상태와 event envelope는 같은 PostgreSQL transaction에서 commit한다.
- relay는 due row를 `FOR UPDATE SKIP LOCKED`로 lease하고, 한 batch의 provider 발행을 동시에 시작해 아직 시도하지 않은 row의 lease 만료를 방지한다.
- Pub/Sub 메시지 body는 `DomainEvent` JSON이며 `event_id`, `event_type`, `schema_version`, `idempotency_key`를 attribute로도 전달한다.
- publish 완료 후 DB 반영이 실패하면 같은 `event_id`가 다시 발행될 수 있으므로 전달 의미는 at-least-once이다.
- consumer는 `(consumer_name, event_id)` receipt를 먼저 기록하고, 같은 event의 중복 효과를 만들지 않는다.
- Outbox 실패는 정제된 오류 분류와 시도 횟수만 저장하고 1초부터 최대 300초까지 지수 backoff로 다시 시도한다. payload나 provider 오류 원문은 운영 오류 필드에 복제하지 않는다.

## 오류와 호환성

공통 fake는 누락된 object key 같은 결정적 오류를 `ProviderError`로 재현한다. `not_found`, `conflict`, `invalid_input`, `unavailable`, `deadline_exceeded`, `permission_denied` 분류와 `retryable` 여부는 provider SDK 타입을 application 계층에 노출하지 않는다. 기존 version 1 필드를 제거하거나 의미를 바꾸지 않으며 optional 필드 추가 또는 새 schema version으로 확장한다.
