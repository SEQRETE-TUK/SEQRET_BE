# 백엔드 2인 작업 분할

> 상태: 확정
>
> 기준일: 2026-08-12
>
> 전제: 단일 백엔드 저장소, 모듈러 모놀리스, 2개 작업 트랙 병렬 진행

## 1. 분할 원칙

- **트랙 A — Product Core & Platform Orchestration:** 업무 원본, API, DB, 상태 전이, 미디어 정책, 이벤트, 알림, 배포 기반을 소유한다.
- **트랙 B — AI & Media Processing Integrations:** GCS, Cloud Tasks, Vertex AI adapter와 실제 처리 파이프라인을 소유한다.
- A는 **무엇을 언제 실행할지** 결정하고, B는 외부 provider에서 **어떻게 실행할지** 구현한다.
- B가 사용하는 port와 데이터 계약을 먼저 main에 병합해 두 트랙이 독립적으로 개발할 수 있게 한다.
- 한 트랙은 다른 트랙의 ORM model이나 repository를 직접 사용하지 않는다.
- PostgreSQL이 업무 상태와 비동기 작업 상태의 유일한 원본이다.
- 고위험 변경은 반드시 상대 담당자가 교차 리뷰한다.

## 2. 소유권

### 트랙 A — Product Core & Platform Orchestration

담당 모듈:

```text
access
move_job
participant
capture
media                 # 도메인, 메타데이터, 권한, API
scope
approval
change_order
completion
audit
notification
background_job
platform/db
platform/cache
platform/event_bus
platform/observability
infrastructure
```

담당 데이터:

- `move_job`
- `job_participant`
- `participant_access_token`
- `location`
- `room_zone`
- `capture_session`
- `media_asset`
- `scope_version`
- `scope_approval`
- `change_order`
- `change_approval`
- `completion_confirmation`
- `audit_event`
- `outbox_event`
- `notification_delivery`
- `background_job`

핵심 책임:

- FastAPI application factory, 공통 설정, 에러 응답과 `ActorContext`
- SQLAlchemy session, 공통 타입, Alembic 운영과 migration 순서 조정
- 작업 생성, 참여자, 위치·구역과 전체 상태 전이
- 비밀 역할 링크 발급, hash 저장, 만료, 철회, rate limit과 사용 이력
- 촬영 세션과 미디어 메타데이터, 목적, 상태, 보존 정책과 접근 권한
- 업로드·열람 URL API의 권한 검사와 `StoragePort` 호출 오케스트레이션
- 부모 버전을 참조하는 불변 `scope_version`과 `content_hash`
- 동일 범위 버전에 대한 양측 확인, 잠금과 동시 수정 방지
- 현장 변경요청, 설명요청, 승인·거절과 결과 버전 연결
- 완료 확인과 append-only 감사 이력
- `background_job` 생성, 상태 전이, 재실행 API와 `TaskQueuePort` 호출
- Transactional Outbox, Pub/Sub 발행과 도메인 event schema
- 알림 intent와 발송 상태 관리
- Redis 캐시·rate limit 정책과 장애 fallback
- Cloud Run 서비스·Job 구성, Terraform, CI/CD와 OpenTelemetry 기본 계측
- 보존기간 삭제·고아 미디어·Outbox 정합성 작업의 정책과 실행 스케줄

### 트랙 B — AI & Media Processing Integrations

담당 모듈:

```text
analysis
media_processing
platform/storage/gcs
platform/task_queue/cloud_tasks
platform/ai/vertex_ai
entrypoints/worker
entrypoints/media_jobs
```

담당 데이터:

- `ai_analysis_run`
- `detection`
- `analysis_location_condition_suggestion`

핵심 책임:

- `StoragePort`의 GCS adapter 구현
- signed upload/read URL 생성, object metadata 조회와 실제 객체 삭제
- 업로드 객체의 MIME type, 크기와 hash 검증
- 썸네일 등 파생 미디어 처리
- `TaskQueuePort`의 Cloud Tasks adapter 구현
- 인증된 private Cloud Run worker 실행 환경
- worker handler의 멱등 실행, retry 분류와 provider 오류 매핑
- `AIProviderPort`의 Vertex AI/Gemini adapter 구현
- AI 입력 구성, prompt·model version과 결과 Pydantic schema
- `ai_analysis_run`, `detection`, `analysis_location_condition_suggestion`과 사람이 검토할 AI 초안 생성
- 장시간 미디어 처리와 물리 삭제를 수행하는 Cloud Run Job handler
- GCS·Cloud Tasks·Vertex AI 실제 adapter contract test
- AI 지연시간, 토큰·비용, task 실패와 미디어 처리 지표 추가

### 공동 계약

구현 소유자는 정해져 있지만 다음 계약 변경은 두 사람의 승인이 필요하다.

- 공통 ID, 시간, 에러 응답과 API version
- 인증된 `ActorContext`
- `StoragePort`, `TaskQueuePort`, `AIProviderPort`, `EventBusPort`
- `MediaAssetRef`와 `AnalysisResult`
- event envelope와 schema version
- `outbox_event` schema와 relay 규칙
- 개인정보·토큰·signed URL 로깅 금지 정책
- production migration과 배포 순서

## 3. A와 B의 상세 경계

| 기능 | A — 정책·오케스트레이션 | B — provider·처리 구현 |
| --- | --- | --- |
| 미디어 업로드 | actor 권한, 목적, 크기 제한, `media_asset` 생성과 완료 상태 | GCS signed URL과 object metadata 조회 |
| 미디어 열람 | 작업·역할·미디어 목적별 접근 허용 판단 | GCS signed read URL 생성 |
| 미디어 삭제 | 보존기간, 삭제 대상 결정, job 상태와 감사 event | GCS 객체 물리 삭제와 결과 반환 |
| AI 분석 | 분석 job 생성, 실행 요청, 결과를 범위 초안으로 가져오기 | Gemini 호출, 결과 검증, `ai_analysis_run`·`detection`·위치 조건 제안 저장 |
| Task Queue | `background_job`과 enqueue 시점, 재실행 정책 | Cloud Tasks 생성과 worker 실행 |
| 장시간 Job | 실행 조건, 스케줄과 대상 범위 | 미디어 처리·삭제 handler |
| Event | Outbox 저장, Pub/Sub 발행, 알림과 업무 consumer | B 모듈 event를 계약에 맞게 생성하고 필요한 handler 제공 |
| 관측성 | 공통 OTel, 대시보드·경보와 배포 계측 | AI·GCS·Task 세부 metric과 span |
| 배포 | Terraform, GitHub Actions, Cloud Run service/job 정의 | worker/job에 필요한 resource·timeout 요구사항 제공 |

AI와 provider adapter는 확정된 작업범위를 직접 변경할 수 없다. 모든 범위 변경은 A의 application command와 업무 검증을 통과해야 한다.

B의 worker와 Job handler도 `background_job`, `media_asset` 등 A 소유 테이블을 직접 갱신하지 않는다. 처리 결과를 A가 공개한 application command로 전달하고, A가 트랜잭션과 감사 event를 반영한다.

## 4. 권장 코드 구조

```text
app/
├─ modules/
│  ├─ access/                 # A
│  ├─ move_job/               # A
│  ├─ capture/                # A
│  ├─ media/                  # A: 도메인·API·권한
│  ├─ scope/                  # A
│  ├─ change_order/           # A
│  ├─ completion/             # A
│  ├─ audit/                  # A
│  ├─ notification/           # A
│  ├─ background_job/         # A
│  ├─ analysis/               # B
│  └─ media_processing/       # B
├─ platform/
│  ├─ db/                     # A
│  ├─ cache/                  # A
│  ├─ event_bus/              # A
│  ├─ observability/          # A 주도, B signal 추가
│  ├─ storage/                # port: 공동 계약, GCS adapter: B
│  ├─ task_queue/             # port: 공동 계약, Cloud Tasks adapter: B
│  └─ ai/                     # B
├─ entrypoints/
│  ├─ api/                    # A
│  ├─ worker/                 # B
│  └─ jobs/                   # 정책·스케줄 A, 처리 handler B
└─ infrastructure/            # A
```

## 5. 먼저 고정할 모듈 간 계약

### 6.1 미디어 계약

```text
MediaAssetRef
- media_asset_id
- job_id
- room_zone_id
- media_purpose
- status
```

`media_purpose`는 최소 다음 값을 지원한다.

```text
inventory
condition
change_evidence
completion
```

A는 `media_asset`과 접근 정책을 소유한다. B의 GCS adapter는 object key를 입력받아 provider 작업만 수행하고 작업 참여자나 역할을 판단하지 않는다.

### 6.2 AI 분석 계약

```text
AnalysisResult
- analysis_run_id
- capture_session_id
- model_name
- model_version
- prompt_version
- result_schema_version
- draft_items
- review_required_items
```

B는 `AnalysisResult`까지만 생성한다. A의 `ImportAnalysisDraft` command가 결과를 검증하고 사람이 수정할 수 있는 새 범위 초안을 만든다.

### 6.3 Event envelope

```text
DomainEvent
- event_id
- event_type
- schema_version
- aggregate_id
- occurred_at
- actor_id
- trace_id
- payload
```

초기 통합 event:

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

event consumer는 `event_id`를 기준으로 중복 처리를 방지한다.

## 6. 실행 계획

### Phase 0 — 공통 기반

공통 기반이 main에 병합된 뒤 같은 commit에서 병렬 작업을 시작한다.

| ID | 담당 | 작업 | 완료 조건 |
| --- | --- | --- | --- |
| FND-A01 | A | FastAPI application factory와 설정 | API·worker·job이 동일 설정 계약으로 실행된다. |
| FND-A02 | A | SQLAlchemy·Alembic·테스트 DB | 빈 DB와 기존 schema에서 migration 검증이 된다. |
| FND-A03 | A | 공통 타입·ActorContext·Port | 두 트랙이 사용할 계약과 fake가 확정된다. |
| FND-A04 | A | CI·Terraform·Cloud Run 골격 | PR 검사와 staging 배포 골격이 실행된다. |
| FND-B01 | B | GCS·Task·AI adapter contract harness | fake와 실제 adapter가 같은 contract test를 사용한다. |

### Phase 1 — 독립 기본 흐름

#### 트랙 A

| ID | 작업 | 선행 작업 | 완료 조건 |
| --- | --- | --- | --- |
| A-01 | 작업·참여자·위치·구역 | FND | 작업 생성 시 초기 참여자 연결 API가 동작한다. |
| A-02 | 비밀 역할 링크와 접근 통제 | A-01 | hash 저장, 만료, 철회와 역할별 접근 테스트가 통과한다. |
| A-03 | 촬영 세션·MediaAsset·업로드 API | A-01, StoragePort | 미디어 목적과 권한 검증 후 adapter를 호출한다. |
| A-04 | 작업범위 초안과 불변 버전 | A-01 | 부모 버전, 순번, content hash와 동시성 테스트가 통과한다. |
| A-05 | 양측 확인과 버전 잠금 | A-04 | 서로 다른 버전 확인과 잠긴 버전 수정을 거부한다. |

#### 트랙 B

| ID | 작업 | 선행 작업 | 완료 조건 |
| --- | --- | --- | --- |
| B-01 | GCS StoragePort adapter | FND-B01 | upload/read/delete contract test가 통과한다. |
| B-02 | Cloud Tasks adapter와 worker runtime | FND-B01 | 인증된 worker가 task를 받아 handler를 실행한다. |
| B-03 | AnalysisRun과 fake AI pipeline | FND-B01 | 구조화된 가짜 AI 결과까지 비동기로 완료된다. |

### Phase 2 — 핵심 기능 확장

#### 트랙 A

| ID | 작업 | 선행 작업 | 완료 조건 |
| --- | --- | --- | --- |
| A-06 | AI 초안 가져오기 | A-04, B-03 계약 | AI 결과가 수정 가능한 범위 초안으로 변환된다. |
| A-07 | 현장 변경요청과 결과 버전 | A-05 | 승인된 변경만 새 범위 버전과 연결된다. |
| A-08 | 완료 확인과 감사 조회 | A-05 | 완료 상태와 주요 변경 이력을 조회할 수 있다. |
| A-09 | Outbox relay·Pub/Sub·알림 | FND-A03 | 미발행 event 재시도, 중복 소비 방지와 알림 상태 추적이 동작한다. |
| A-10 | Redis·rate limit·fallback | A-02 | Redis 장애 시 원본 DB 기반 제한 동작이 가능하다. |
| A-11 | Cloud Run·Terraform·CI/CD·OTel | FND-A04 | staging 배포, rollback과 핵심 경보가 검증된다. |
| A-12 | 삭제·재처리·정합성 정책 | A-03, A-09 | 대상 결정, job 생성과 재실행 API가 동작한다. |
| A-13 | 배차·현장 브리프·체크인 | A-05, A-09 | 현재 범위에 묶인 자원 snapshot으로 배차를 확정하고 대표 현장기사가 당일 체크인한다. |
| A-14 | 기사 승인본 view와 이력 권한 | A-05, A-13 | 기사는 잠긴 범위·확정 견적만 보고 전체 초안 이력은 조회하지 못한다. |
| A-15 | 구조화 작업범위 품목 v2 | A-04, A-06 | 기존 v1을 유지하면서 이름·수량·단위·작업 메모가 불변 snapshot에 저장된다. |
| A-16 | 출·도착지 구조화 조건 | A-04, A-15 | 주거·층·엘리베이터·계단·주차·운반거리 조건이 값 또는 모름으로 저장되고 v2 범위에 고정된다. |
| A-17 | 한 장 공동확인 카드 상태 | A-16, INT-03 | 업체 참여·협업 상태, 범위 hash·총액·확인 시각과 승인된 변경이 화면 조회 하나로 복원된다. |
| A-18 | 업체 현장 이슈 시작 | A-17, INT-03 | 업체와 현장기사가 각자 소유한 증거로 이슈를 시작하고 금액 제안·고객 결정 권한은 분리된다. |
| A-19 | 미디어 처리 명시 동의 | A-03, A-12 | 현재 동의문·처리 목적·보관기간을 공개하고 정책 버전·확인 시각을 촬영 세션에 불변 기록한다. |
| A-20 | AI 품목·위치 조건 v2 공통 계약 | A-15, A-16 | 기존 v1과 호환하면서 수량·단위·작업 메모, 비주소 source context와 추가금 위험 위치 조건을 version 2 결과로 검증한다. |
| A-21 | AI v2 범위 가져오기와 고객 검수 | A-20 | v2 결과를 구조화된 미확정 범위로 가져오고 고객이 품목·위치 조건을 검수한 새 버전만 생성한다. |
| A-22 | 범위 조회 schema version 명시 | A-15, A-21 | 공동확인·기사 브리프가 현재 범위의 v1/v2 schema version을 명시한다. |
| A-23 | 업체 견적 실행계획 snapshot | A-17 | 차량 수·규격, 작업자 수, 예상시간이 견적 버전과 함께 고정된다. |
| A-24 | 변경 승인본 기사 견적 복원 | A-14, A-18 | 현장 변경 승인 뒤 기사 브리프가 새 누적 견적과 기존 포함·제외 작업을 함께 반환한다. |

#### 트랙 B

| ID | 작업 | 선행 작업 | 완료 조건 |
| --- | --- | --- | --- |
| B-04 | Vertex AI/Gemini adapter | B-03 | 모델 출력이 버전이 있는 Pydantic schema로 검증된다. |
| B-05 | 미디어 검증과 파생 처리 정책 | B-01 | v1은 크기·MIME·hash와 generation을 검증하고 원본만 사용한다. 파생 파일은 실제 소비 화면이 생길 때 별도 versioned 계약으로 추가한다. |
| B-06 | worker 멱등성과 오류 매핑 | B-02, B-04 | 중복 실행과 재시도 가능한 오류가 안전하게 처리된다. |
| B-07 | GCS 삭제·장시간 Job handler | B-01, B-02 | A가 지정한 대상만 삭제하고 결과를 반환한다. |
| B-08 | AI v2 영속화와 Vertex 출력 | A-20, A-21, B-04 | 구현 완료. 수량·단위·작업 메모와 위치 조건 제안을 저장·복원하고 Vertex 결과를 v2 계약으로 검증한다. |

### Phase 3 — 공동 통합

| ID | 주도 | 시나리오 | 완료 조건 |
| --- | --- | --- | --- |
| INT-01 | B | 촬영 제출 → AI 분석 → 범위 초안 | 중복 event와 AI 실패에서도 수동 작업이 가능하다. |
| INT-02 | A | 양측 확인 → 범위 잠금 | 서로 다른 버전 확인과 동시 수정이 차단된다. |
| INT-03 | A | 변경 증거 → 변경요청 → 새 버전 | 미디어 권한과 결과 버전 연결이 유지된다. |
| INT-04 | A | 완료 제출 → 업체 요청 → 고객 확인·문제 신고 → 문서·보존 정책 | 구현 완료. 자동 책임 판단 없이 정정 가능한 불변 완료 기록, 결정적 문서 archive와 선택적 미디어 삭제 일정이 연결된다. |
| INT-05 | A | 토큰 만료·철회·권한 공격 | 다른 작업과 역할의 데이터에 접근할 수 없다. |
| INT-06 | B | task 재시도·provider 장애·복구 | 동일 작업이 반복돼도 결과가 한 번만 반영된다. |
| INT-07 | A | 배포·migration·복구 | staging 배포와 DB 복구 절차가 재현된다. |

## 7. 브랜치와 migration 전략

상세한 동시 작업, AI agent, 공용 파일과 충돌 해결 규칙은 [`CONCURRENT_DEVELOPMENT_GUIDE.md`](CONCURRENT_DEVELOPMENT_GUIDE.md)를 단일 기준으로 사용한다.

- `develop`, `track-a`, `track-b` 같은 장기 브랜치를 만들지 않는다.
- Phase 0 공통 기반 merge 후 두 사람은 같은 main commit에서 시작한다.
- 작업 ID마다 짧은 branch와 PR을 만든다.
- branch 예시는 `feat/a-04-scope-version`, `feat/b-01-gcs-adapter`를 사용한다.
- main 직접 push를 금지하고 상대 담당자의 승인 1개와 CI 통과를 요구한다.
- merge 방식은 Squash Merge로 통일하고 merge 후 branch를 삭제한다.
- 공개 Port나 event 계약을 바꾸는 PR은 구현 PR보다 먼저 병합한다.
- B는 A의 미병합 branch를 기반으로 개발하지 않고 main의 contract와 fake를 사용한다.
- A가 Alembic merge 순서를 조정한다. B 소유 모델의 migration도 A의 승인을 받아 선형 revision으로 병합한다.
- 동시에 열린 migration PR이 있으면 먼저 병합된 revision 기준으로 나머지 branch를 rebase하고 `down_revision`을 갱신한다.

## 8. 교차 리뷰 필수 영역

- 역할 링크, 권한과 개인정보 접근
- `scope_version`, `content_hash`, 양측 확인과 상태 전이
- `StoragePort`, `TaskQueuePort`, `AIProviderPort` 계약
- AI 결과를 범위 초안으로 가져오는 경계
- task와 event 멱등성
- Outbox transaction과 relay
- signed upload/read URL 정책
- 미디어 삭제와 감사 데이터 보존
- DB migration과 production 배포 순서

## 9. 완료 기준

각 작업은 다음 조건을 모두 만족해야 완료로 본다.

- 성공 흐름과 권한 거부·중복 실행·재시도 실패 테스트가 있다.
- 필요한 Alembic migration과 rollback 검증이 있다.
- OpenAPI, Port 또는 event schema 변경이 문서에 반영됐다.
- 구조화 로그와 필요한 metric이 추가됐다.
- 토큰, 주소 원문, signed URL과 원본 미디어가 로그에 남지 않는다.
- 다른 모듈의 ORM model이나 repository를 직접 사용하지 않는다.
- 운영자가 실패 상태를 확인하고 안전하게 재실행할 수 있다.
- 상대 담당자의 리뷰가 완료됐다.

## 10. 최종 책임 경계

```text
트랙 A가 소유하는 것
제품의 업무 사실, API, DB, 권한, 상태 전이, 미디어 정책,
범위 버전, 확인, 변경, 감사, 이벤트, 알림과 배포 기반

트랙 B가 소유하는 것
GCS·Cloud Tasks·Vertex AI provider adapter,
AI 분석과 미디어 처리 및 worker handler

절대 넘지 않는 경계
B는 확정 범위와 A 소유 업무 상태를 직접 변경하지 않는다.
A는 GCP provider SDK를 직접 호출하지 않고 Port를 통해 B adapter를 사용한다.
```
