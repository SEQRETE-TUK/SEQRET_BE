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

## 오류와 호환성

공통 fake는 누락된 object key 같은 결정적 오류를 `ProviderError`로 재현한다. `not_found`, `conflict`, `invalid_input`, `unavailable`, `deadline_exceeded`, `permission_denied` 분류와 `retryable` 여부는 provider SDK 타입을 application 계층에 노출하지 않는다. 기존 version 1 필드를 제거하거나 의미를 바꾸지 않으며 optional 필드 추가 또는 새 schema version으로 확장한다.
