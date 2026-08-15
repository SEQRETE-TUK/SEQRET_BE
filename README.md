# SEQRET Backend

이사 작업범위 공동확인 서비스 **SEQRET**의 백엔드 저장소입니다.

작업 생성, 비공개 미디어, AI 초안, 작업범위 버전, 양측 확인, 현장 변경요청과 감사 이력을 하나의 일관된 시스템으로 관리합니다.

## 기술 스택

| 구분 | 기술 |
| --- | --- |
| Backend | Python 3.13, FastAPI, Pydantic |
| Data Access | SQLAlchemy, Alembic |
| Database | PostgreSQL, Cloud SQL |
| Cache | Redis, Memorystore |
| Object Storage | Google Cloud Storage |
| Compute | Docker, Cloud Run |
| Async | Cloud Tasks, Cloud Run Jobs |
| Event / Schedule | Pub/Sub, Cloud Scheduler |
| AI | Vertex AI, Gemini |
| Security | Cloud Armor, Secret Manager, Cloud IAM |
| Infrastructure | Terraform, Cloud Load Balancing |
| CI/CD | GitHub Actions, Artifact Registry |
| Observability | OpenTelemetry, Cloud Logging, Cloud Monitoring |

정확한 라이브러리와 컨테이너 버전은 프로젝트 초기화 시 lockfile과 이미지 digest로 고정합니다.

## 아키텍처

- 하나의 코드베이스를 도메인별로 분리한 **모듈러 모놀리스**로 시작한다.
- API, 비동기 worker, 장시간·정기 job은 실행 단위를 분리한다.
- 모든 업무 원본 상태와 비동기 작업 상태는 PostgreSQL을 기준으로 한다.
- 사진과 영상은 비공개 객체 스토리지에 저장하고 데이터베이스에는 object key와 메타데이터만 저장한다.
- AI 출력은 사람이 검토·수정할 수 있는 초안이며, 확인된 작업범위의 원본이 될 수 없다.

## 문서

- [기술 스택 및 아키텍처 결정](docs/TECH_STACK.md)
- [백엔드 2인 작업 분할](docs/BACKEND_WORK_SPLIT.md)
- [동시 개발 및 main 머지 충돌 방지 가이드](docs/CONCURRENT_DEVELOPMENT_GUIDE.md)
- [공통 타입·Port·event 계약](docs/CONTRACTS.md)
- [현재 main 팀·FE 인계](docs/TEAM_HANDOFF.md)
- [AI 작업 지침](AGENTS.md)

## 상태

FastAPI·PostgreSQL 기반, 두 트랙의 공통 계약과 작업·참여자·위치·구역 API를 구성했습니다.

업무 API는 설정된 API prefix(기본 `/api/v1`) 아래에서 제공됩니다.

- `POST /move-jobs/onboarding`: 소비자 작업과 소비자 전용 비밀 링크 생성
- `GET /me`: 현재 역할·초대 상태·허용 기능 조회
- `POST·GET /move-jobs/{job_id}/invitations`: 다음 역할 초대 발급·목록 조회
- `POST /move-jobs/{job_id}/invitations/{invitation_id}/{accept|decline|revoke|reissue}`: 초대 수락·거절·폐기·재발급
- `POST /move-jobs`: 신뢰 bootstrap용 작업·세 역할·위치·구역 생성
- `GET /move-jobs/{job_id}`: 전체 작업 구성 조회
- `POST /move-jobs/{job_id}/participants/{participant_id}/access-links`: 자기 역할 링크 회전
- `POST /move-jobs/{job_id}/access-links/{access_link_id}/revoke`: 역할 링크 철회
- `POST /move-jobs/{job_id}/capture-sessions`: 참여자 소유 촬영 세션 생성
- `GET /move-jobs/{job_id}/capture-sessions`: 본인 촬영 세션·미디어·분석 상태 복구
- `POST /move-jobs/{job_id}/capture-sessions/{capture_session_id}/submit`: READY 촬영 제출과 분석 요청
- `GET /move-jobs/{job_id}/capture-sessions/{capture_session_id}/analysis`: 촬영 분석·범위 초안 상태 조회
- `POST /move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/upload`: 업로드 URL 발급
- `POST /move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/{media_asset_id}/complete`: 업로드 메타데이터 확인
- `POST /move-jobs/{job_id}/scope-versions`: 불변 작업범위 버전 생성
- `GET /move-jobs/{job_id}/scope-versions`: 작업범위 버전 이력 조회
- `POST /move-jobs/{job_id}/scope-versions/{scope_version_id}/approvals`: 작업범위 버전 확인
- `GET /move-jobs/{job_id}/scope-review`: 현재 범위·견적·수정요청·양측 확인 화면 조회
- `POST /move-jobs/{job_id}/scope-proposals`: 업체 범위·원화 견적 제안
- `POST /move-jobs/{job_id}/scope-review/revision-request`: 고객 범위 수정 요청
- `POST /move-jobs/{job_id}/scope-review/confirm`: 고객 현재 제안 확인과 범위 잠금
- `POST /move-jobs/{job_id}/dispatch/setup`: 업체 연동의 작업별 요구사항·차량·인력 snapshot 등록
- `GET·PUT /move-jobs/{job_id}/dispatch`: 배차 후보·충돌 조회와 원자적 배차 확정
- `GET /move-jobs/{job_id}/field-brief`: 배정 현장기사의 확정 범위·일정·현장 조건 조회
- `POST /move-jobs/{job_id}/check-ins`: 배정 현장기사의 당일 checklist 확인과 체크인
- `POST·GET /move-jobs/{job_id}/field-issues`: 현장기사 이슈 보고와 업체·현장기사 처리 상태 조회
- `POST /move-jobs/{job_id}/change-proposals`: 업체 변경 범위·원화 견적 제안
- `GET /move-jobs/{job_id}/change-proposals/{proposal_id}`: 고객·업체용 변경 사유·증거·견적 화면 조회
- `POST /move-jobs/{job_id}/change-proposals/{proposal_id}/decision`: 고객 승인·거절·설명 요청
- `POST /move-jobs/{job_id}/change-proposals/{proposal_id}/explanation`: 업체 변경 설명 제출
- `POST /move-jobs/{job_id}/change-requests`: 현장 변경요청 생성
- `GET /move-jobs/{job_id}/change-requests`: 현장 변경요청 이력 조회
- `GET /move-jobs/{job_id}/change-requests/{change_request_id}/evidence/{media_asset_id}/read-url`: 고객·회사 관리자용 변경 증거 열람 URL 발급
- `POST /move-jobs/{job_id}/change-requests/{change_request_id}/clarification`: 설명 요청
- `POST /move-jobs/{job_id}/change-requests/{change_request_id}/explanation`: 설명 제출
- `POST /move-jobs/{job_id}/change-requests/{change_request_id}/decision`: 승인 또는 거절
- `POST /move-jobs/{job_id}/completion-submissions`: 대표 현장기사의 체크리스트·근무·선택적 완료 미디어 제출
- `GET /move-jobs/{job_id}/completion-summary`: 업체·요청받은 고객의 완료·금액·문서 상태 조회
- `POST /move-jobs/{job_id}/completion-requests`: 업체의 7일 고객 완료 확인 요청
- `POST /move-jobs/{job_id}/completion-requests/{request_id}/revoke`: 미결 완료 요청 철회
- `POST /move-jobs/{job_id}/completion-requests/{request_id}/decision`: 고객 완료 확인 또는 문제 신고
- `GET /move-jobs/{job_id}/documents/archive`: 업체용 결정적 PDF 4종·manifest ZIP 다운로드
- `POST /move-jobs/{job_id}/completion-confirmations`: 작업 완료 확인
- `GET /move-jobs/{job_id}/completion-confirmations`: 작업 완료 확인 이력 조회
- `GET /move-jobs/{job_id}/audit-events`: 작업 감사 이력 조회
- `GET /move-jobs/{job_id}/notifications`: 현재 참여자의 알림 전달 상태 조회

위치에는 주소 원문 대신 화면 표시용 label만 저장합니다.

소비자 self-service 생성은 `POST /move-jobs/onboarding`을 사용합니다. 이 응답은 최초 발급부터 7일 동안 유효한 소비자 비밀값 하나만 한 번 반환하며, 업체나 현장기사 capability를 미리 만들지 않습니다. 소비자만 업체 담당자를 초대하고 수락한 업체 담당자만 현장기사를 초대할 수 있습니다. 초대가 `pending`인 링크는 `/me`와 수락·거절 외 업무 API를 열 수 없으며, 폐기·재발급은 기존 secret과 그 하위 초대를 즉시 무효화합니다. 하위 링크의 만료는 발급자 링크 만료를 넘지 않습니다. 기존 `POST /move-jobs`는 정확히 세 역할을 한 번에 준비하는 신뢰 bootstrap 계약으로 남아 일반 frontend 생성 흐름에서 사용하지 않습니다. bearer 링크 자체는 개인 신원을 증명하지 않습니다. 비밀값은 `Authorization: Bearer <secret>`으로 전달하며 데이터베이스에는 SHA-256 hash만 저장합니다. 비밀값을 반환하는 응답은 cache하지 않습니다.

촬영 미디어는 작업에 속한 구역과 `inventory`, `condition`, `change_evidence`, `completion` 목적 중 하나로 등록할 수 있습니다. 변경·완료 command는 목적과 촬영자 역할을 다시 검증합니다. 사진은 20 MiB, 영상은 200 MiB로 제한하며, 업로드 완료 시 `StoragePort`로 MIME type, 정확한 크기와 object generation을 다시 확인합니다. 완료·취소된 작업에는 새 촬영·업로드를 허용하지 않습니다. 비공개 객체의 signed URL과 create-only `upload_headers`는 HTTPS 응답으로만 반환하고 값은 정규화하지 않으며 cache, 데이터베이스와 로그에는 저장하지 않습니다. 로컬에서 storage 설정을 생략하면 업로드 API는 `503`을 반환하고, 배포 API는 storage 설정 없이는 시작하지 않습니다.

촬영 소유자는 `GET /capture-sessions`로 자신이 만든 세션, 미디어의 `pending_upload|uploaded|processing|ready|failed|deleted` 상태와 선택적 분석 상태를 복구할 수 있습니다. 응답에는 object key, generation, signed URL과 provider task ID가 포함되지 않습니다. 하나 이상의 `READY` inventory 미디어가 준비된 뒤 촬영 세션을 한 번 제출합니다. 제출과 `capture_submitted.v1`은 같은 transaction에 기록되며 이후 해당 세션의 새 미디어 변경은 막힙니다. 매분 relay가 분석 intent를 멱등 Cloud Task로 전달하고 private worker가 B의 분석 결과를 만든 뒤 A의 `import_analysis_draft` command로 편집 가능한 범위 초안을 생성합니다. provider 오류나 안전하게 가져올 수 없는 결과는 provider-neutral 실패 상태로 남고 수동 범위 작성은 계속 가능합니다.

작업범위 편집은 기존 row를 덮어쓰지 않고 현재 버전을 부모로 삼는 새 snapshot을 생성합니다. 각 버전은 작업별 순번과 canonical JSON의 SHA-256 content hash를 가지며, 한 부모에서 두 갈래 버전이 생기지 않도록 데이터베이스 제약으로 선형 이력을 유지합니다.

고객과 회사 관리자가 같은 현재 버전을 각각 확인하면 해당 버전이 잠깁니다. 이미 다음 버전이 있는 과거 버전, 중복 확인, 잠긴 버전의 후속 편집은 거부합니다.

일반 frontend의 범위 화면은 저수준 scope API 대신 `scope-review` 흐름을 사용합니다. 업체 제안은 고객이 작성한 현재 범위 또는 고객 수정요청이 열린 현재 제안을 source로 새 불변 자식 version과 원화 견적 snapshot을 만들고 업체 확인을 함께 기록합니다. 고객은 수정요청 또는 확인 중 하나를 선택하며, 확인되면 기존 scope lock·Outbox·감사 계약을 재사용합니다. 같은 source와 정확히 같은 command 재전송은 기존 결과를 반환하고 다른 payload나 stale source는 거부합니다. 범위 조회의 AI 원본 사진은 READY 객체의 짧은 generation-pinned signed URL만 제공하며 provider 내부값은 노출하지 않습니다. 현재 범위 schema v1은 항목 key·공간·설명을 지원하고 수량·단위·작업 메모는 후속 AI schema v2 범위입니다.

내부 `import_analysis_draft` application command는 공용 `AnalysisResult`를 검증해 새 작업범위 버전으로 변환합니다. AI 제안의 출처 미디어와 구역을 확인하고 모델·프롬프트·confidence·검토 필요 여부를 provenance로 보존하며, 같은 analysis run은 한 번만 가져옵니다. 외부 HTTP에서 raw AI 결과를 직접 등록하는 경로는 제공하지 않습니다.

일반 frontend의 현장 변경 흐름은 역할을 분리합니다. 현장 작업자는 잠긴 현재 범위와 자신이 업로드 완료한 `UPLOADED|READY` `change_evidence`를 기준으로 무가격 이슈를 보고합니다. 회사 관리자는 모든 증거의 validation이 `READY`가 된 이슈 하나를 현재 확정 금액 기준의 변경 범위·견적 제안으로 전환하고, 고객은 generation-pinned 증거 preview를 확인한 뒤 승인·거절·설명 요청 중 하나를 선택합니다. 설명 요청에는 업체가 한 번 설명을 제출할 수 있습니다. 승인된 제안은 기존 변경요청과 범위 승인 로직을 재사용해 결과 범위를 만들고 양측 확인과 잠금을 같은 command 안에서 완료합니다. 같은 client reference 또는 동일 command 재전송은 같은 결과를 반환하고 stale 범위나 상충 payload는 거부합니다. 기존 `change-requests` route는 신뢰 bootstrap·내부 호환 계약으로 남습니다.

배차는 회사 관리자가 현재 잠긴 leaf 범위와 작업 일정에 묶인 차량·작업자 가용성 snapshot을 먼저 한 번 등록한 뒤 확정합니다. 서버는 차량 용량, 요구 인원, 일정, 필수 기술·자격과 대표 현장기사 배정을 다시 검증하고 `dispatch_confirmed.v1`을 같은 transaction에 기록합니다. 현장기사는 확정된 배차와 최신 범위에 대해서만 마스킹 위치·현장 조건·안전 checklist를 조회하고 작업 예정일 당일 모든 항목을 확인해 체크인할 수 있습니다. 연락처·채팅·지도 원본이 없으므로 관련 URI는 임의 생성하지 않고 `null`로 반환합니다.

대표 현장기사는 체크인된 현재 배차·잠긴 범위에 대해 setup의 완료 checklist 전체, 실제 근무 구간, 현장 고객 확인 시각과 선택적 `READY` completion 미디어를 `completion-submissions`에 불변 기록합니다. 업체는 최신 제출만 7일 유효한 고객 확인 요청으로 보낼 수 있고, 고객은 확인하거나 별도 문제 기록을 남깁니다. 문제 신고는 책임을 자동 판단하지 않으며 정정 제출과 새 요청을 허용합니다. 고객 확인 시 고객·업체 완료 확인, 작업 `completed` 전이, generation-pinned 미디어 보존 삭제 intent와 `completion_decided.v1`을 같은 transaction에 기록합니다. 미디어가 없는 완료도 허용하며 이 경우 삭제 intent는 0개입니다. 기존 양측 `completion-confirmations` route는 신뢰 bootstrap·호환 계약으로 남습니다.

완료 요약은 최종 견적, 승인된 현장 변경, 체크리스트, 근무, 선택적 미디어 preview, 요청·문제 상태와 보존기한을 한 view로 반환합니다. 문서가 준비되면 서버가 외부 PDF 의존성 없이 견적서·변경 승인 기록·작업 완료 기록·완료 확인 기록과 JSON manifest를 결정적 ZIP으로 생성합니다. 완료 사실은 문서 생성 또는 storage read URL 실패와 독립적으로 DB에 보존됩니다.

촬영 제출, 분석 완료·실패, 범위 잠금, 현장 변경요청, 배차 확정, 완료 미디어 등록, 완료 제출·요청·고객 결정은 업무 트랜잭션 안에서 Outbox event를 저장합니다. `python -m app.entrypoints.outbox_relay`를 한 번 실행하면 due event를 lease로 선점해 Pub/Sub에 발행하고 알림 subscription의 bounded batch를 pull한 뒤, due 미디어·분석 intent를 Cloud Tasks에 전달하는 event pump로 동작합니다. 실패한 발행·enqueue는 지수 backoff로 다시 시도되고, 소비자는 `event_id`별 영속 receipt로 중복 효과를 막습니다. 참여자별 알림 intent에는 연락처나 메시지 원문 없이 발송 상태와 정제된 오류 코드만 저장합니다.

같은 scheduled relay Job이 Pub/Sub `DomainEvent`를 `consume_notification_event` application command에 전달하고 DB commit 뒤 ack합니다. 최초 31일 replay와 DLQ 운영 절차는 [`docs/NOTIFICATION_CONSUMER_RUNBOOK.md`](docs/NOTIFICATION_CONSUMER_RUNBOOK.md)를 따릅니다. 실제 연락처 해석과 이메일·메시지 provider 호출은 destination 계약이 없어 이 런타임에 포함하지 않으며 현재는 `PENDING` in-app intent까지만 생성합니다.

## 로컬 개발

Python 버전과 dependency는 `uv`로 고정합니다.
패키지 버전은 `app/__init__.py`의 `__version__`을 단일 원본으로 사용합니다.

```bash
uv sync --dev
uv run uvicorn app.entrypoints.api:app --reload
```

기본 설정은 `SEQRET_` 접두사의 환경변수로 덮어쓸 수 있습니다. 로컬 설정은 `.env.example`을 참고하되 실제 `.env` 파일과 비밀값은 commit하지 않습니다.

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SEQRET_APP_NAME` | `SEQRET Backend` | API 문서와 application에 표시할 이름 |
| `SEQRET_SERVICE_NAME` | `seqret` | 소문자 DNS label 형식의 runtime 공통 식별자. suffix 제외 최대 42자, 최종 Cloud Run 이름 최대 49자 |
| `SEQRET_ENVIRONMENT` | `local` | `local`, `test`, `staging`, `production` 중 하나 |
| `SEQRET_DEBUG` | `false` | FastAPI debug 여부. production에서는 `true`를 거부함 |
| `SEQRET_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` 중 하나 |
| `SEQRET_API_PREFIX` | `/api/v1` | 향후 업무 API가 사용할 정규화된 절대 경로 접두사 |
| `SEQRET_FRONTEND_ORIGIN` | 없음 | API CORS가 허용할 canonical HTTPS browser origin 하나. port, path, wildcard는 허용하지 않음 |
| `SEQRET_MEDIA_BUCKET_NAME` | 없음 | API가 사용할 기존 private Cloud Storage bucket 이름 |
| `SEQRET_STORAGE_SIGNING_SERVICE_ACCOUNT_EMAIL` | 없음 | signed URL을 발급할 API service account email. bucket과 함께 설정 |
| `SEQRET_DATABASE_URL` | 없음 | `postgresql+psycopg` 형식의 비밀 연결 URL. 엔진 생성과 migration 실행 시 필수 |
| `SEQRET_DATABASE_POOL_SIZE` | `5` | runtime별 기본 connection pool 크기 |
| `SEQRET_DATABASE_MAX_OVERFLOW` | `10` | pool 크기를 초과해 일시적으로 허용할 connection 수 |
| `SEQRET_DATABASE_POOL_TIMEOUT_SECONDS` | `30` | pool에서 connection을 기다리는 최대 시간 |
| `SEQRET_REDIS_URL` | 없음 | `redis://` 또는 TLS `rediss://` 연결 URL. PostgreSQL 제한 구간을 원본으로 유지하며 Redis는 같은 구간의 보조 제한 적용 |
| `SEQRET_REDIS_MAX_CONNECTIONS` | `10` | API process가 공유하는 Redis connection pool 상한 |
| `SEQRET_CACHE_TIMEOUT_SECONDS` | `0.2` | Redis 카운터 호출을 기다리는 최대 시간 |
| `SEQRET_ACCESS_RATE_LIMIT_REQUESTS` | `120` | 유효한 역할 링크 하나가 고정 구간에서 허용받는 요청 수 |
| `SEQRET_ACCESS_RATE_LIMIT_WINDOW_SECONDS` | `60` | 역할 링크별 고정 제한 구간 길이 |
| `SEQRET_PUBSUB_PROJECT_ID` | 없음 | Outbox relay가 발행할 Pub/Sub project. topic과 함께 설정 |
| `SEQRET_PUBSUB_TOPIC_ID` | 없음 | versioned domain event Pub/Sub topic ID. project와 함께 설정 |
| `SEQRET_PUBSUB_SUBSCRIPTION_ID` | 없음 | 같은 relay event pump가 pull할 notification subscription ID. project와 topic 필요 |
| `SEQRET_OUTBOX_BATCH_SIZE` | `100` | relay 한 번에 lease하고 동시에 발행할 최대 event 수 |
| `SEQRET_OUTBOX_LEASE_SECONDS` | `60` | 발행 worker의 event lease 시간. publish timeout보다 길어야 함 |
| `SEQRET_EVENT_PUBLISH_TIMEOUT_SECONDS` | `10` | 개별 Pub/Sub 발행 완료를 기다리는 최대 시간 |
| `SEQRET_NOTIFICATION_BATCH_SIZE` | `100` | event pump 한 번에 pull할 최대 notification event 수 |
| `SEQRET_NOTIFICATION_PULL_TIMEOUT_SECONDS` | `10` | notification pull이 빈 batch를 기다리는 최대 시간 |
| `SEQRET_TASK_QUEUE_LOCATION` | 없음 | Cloud Tasks queue region. task 설정과 함께 relay에 구성 |
| `SEQRET_TASK_QUEUE_NAME` | 없음 | media validation·retention task queue ID |
| `SEQRET_TASK_WORKER_URL` | 없음 | OIDC로 호출하는 canonical HTTPS private worker origin |
| `SEQRET_TASK_INVOKER_SERVICE_ACCOUNT_EMAIL` | 없음 | Cloud Tasks OIDC 전용 service account |
| `SEQRET_BACKGROUND_JOB_BATCH_SIZE` | `100` | relay 실행당 enqueue할 최대 background job 수 |
| `SEQRET_BACKGROUND_JOB_LEASE_SECONDS` | `60` | enqueue 소유권 lease. enqueue timeout보다 길어야 함 |
| `SEQRET_TASK_ENQUEUE_TIMEOUT_SECONDS` | `10` | 개별 Cloud Tasks 생성 제한 시간 |
| `SEQRET_MEDIA_RETENTION_DAYS` | 없음 | 완료 작업 미디어의 승인된 보존기간. 비설정 시 완료 확인과 삭제 작업 생성을 차단함 |

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

DB migration과 테스트 DB 운영 방법은 [데이터베이스 개발 가이드](docs/DATABASE.md)를 따릅니다.

API가 정상적으로 bootstrap됐는지는 `GET /healthz`로 확인합니다.

- health 응답은 중간 cache가 이전 상태를 재사용하지 않도록 `Cache-Control: no-store`를 반환합니다.
- production 환경에서는 `/docs`, `/redoc`, `/openapi.json` HTTP endpoint를 노출하지 않습니다.
