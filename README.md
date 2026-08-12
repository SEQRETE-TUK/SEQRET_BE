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

- `POST /move-jobs`: 작업과 초기 참여자·위치·구역 생성
- `GET /move-jobs/{job_id}`: 전체 작업 구성 조회
- `POST /move-jobs/{job_id}/participants/{participant_id}/access-links`: 자기 역할 링크 회전
- `POST /move-jobs/{job_id}/access-links/{access_link_id}/revoke`: 역할 링크 철회
- `POST /move-jobs/{job_id}/capture-sessions`: 참여자 소유 촬영 세션 생성
- `POST /move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/upload`: 업로드 URL 발급
- `POST /move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/{media_asset_id}/complete`: 업로드 메타데이터 확인
- `POST /move-jobs/{job_id}/scope-versions`: 불변 작업범위 버전 생성
- `GET /move-jobs/{job_id}/scope-versions`: 작업범위 버전 이력 조회
- `POST /move-jobs/{job_id}/scope-versions/{scope_version_id}/approvals`: 작업범위 버전 확인
- `POST /move-jobs/{job_id}/change-requests`: 현장 변경요청 생성
- `GET /move-jobs/{job_id}/change-requests`: 현장 변경요청 이력 조회
- `GET /move-jobs/{job_id}/change-requests/{change_request_id}/evidence/{media_asset_id}/read-url`: 고객·회사 관리자용 변경 증거 열람 URL 발급
- `POST /move-jobs/{job_id}/change-requests/{change_request_id}/clarification`: 설명 요청
- `POST /move-jobs/{job_id}/change-requests/{change_request_id}/explanation`: 설명 제출
- `POST /move-jobs/{job_id}/change-requests/{change_request_id}/decision`: 승인 또는 거절
- `POST /move-jobs/{job_id}/completion-confirmations`: 작업 완료 확인
- `GET /move-jobs/{job_id}/completion-confirmations`: 작업 완료 확인 이력 조회
- `GET /move-jobs/{job_id}/audit-events`: 작업 감사 이력 조회
- `GET /move-jobs/{job_id}/notifications`: 현재 참여자의 알림 전달 상태 조회

위치에는 주소 원문 대신 화면 표시용 label만 저장합니다.

작업 생성 응답은 최초 발급부터 7일 동안 유효한 초기 역할 링크의 비밀값을 한 번만 반환합니다. 이 공개 bootstrap 호출자는 모든 초기 역할 capability를 각 참여자에게 안전하게 전달하는 신뢰 주체이며, bearer 링크 자체는 개인 신원을 증명하지 않습니다. 고객·회사 관리자·현장 작업자 세 역할은 작업 생성 시 모두 연결하며, 검증된 별도 전달 채널이 없는 현재 public API에서는 이후 참여자를 추가하지 않습니다. 이후 작업 API는 비밀값을 `Authorization: Bearer <secret>`으로 받으며 데이터베이스에는 SHA-256 hash만 저장합니다. 각 참여자는 자기 링크만 회전할 수 있고, 회전은 같은 링크의 비밀값만 교체하므로 최초 절대 만료와 rate-limit 구간을 연장하거나 초기화하지 않습니다. 비밀값을 반환하는 응답은 cache하지 않습니다.

촬영 미디어는 작업에 속한 구역과 `inventory`, `condition`, `change_evidence`, `completion` 목적 중 하나로 등록할 수 있습니다. 변경·완료 command는 목적과 촬영자 역할을 다시 검증합니다. 사진은 20 MiB, 영상은 200 MiB로 제한하며, 업로드 완료 시 `StoragePort`로 MIME type, 정확한 크기와 object generation을 다시 확인합니다. 완료·취소된 작업에는 새 촬영·업로드를 허용하지 않습니다. 비공개 객체의 signed URL과 create-only `upload_headers`는 HTTPS 응답으로만 반환하고 값은 정규화하지 않으며 cache, 데이터베이스와 로그에는 저장하지 않습니다. 실제 스토리지 adapter가 구성되지 않은 환경에서는 업로드 API가 `503`을 반환합니다.

작업범위 편집은 기존 row를 덮어쓰지 않고 현재 버전을 부모로 삼는 새 snapshot을 생성합니다. 각 버전은 작업별 순번과 canonical JSON의 SHA-256 content hash를 가지며, 한 부모에서 두 갈래 버전이 생기지 않도록 데이터베이스 제약으로 선형 이력을 유지합니다.

고객과 회사 관리자가 같은 현재 버전을 각각 확인하면 해당 버전이 잠깁니다. 이미 다음 버전이 있는 과거 버전, 중복 확인, 잠긴 버전의 후속 편집은 거부합니다.

내부 `import_analysis_draft` application command는 공용 `AnalysisResult`를 검증해 새 작업범위 버전으로 변환합니다. AI 제안의 출처 미디어와 구역을 확인하고 모델·프롬프트·confidence·검토 필요 여부를 provenance로 보존하며, 같은 analysis run은 한 번만 가져옵니다. 외부 HTTP에서 raw AI 결과를 직접 등록하는 경로는 제공하지 않습니다.

현장 작업자는 잠긴 현재 범위를 기준으로 자신이 촬영한 `change_evidence` 미디어와 변경안을 제출할 수 있습니다. 고객 또는 회사 관리자는 한 번 설명을 요청한 뒤 승인하거나 사유와 함께 거절합니다. 승인된 요청만 기준 범위의 결과 버전을 만들며, 결과 버전은 다시 양측 확인을 받아야 잠깁니다.

현장 작업자가 업로드한 `completion` 증거와 현재 잠긴 범위를 고객과 회사 관리자가 각각 확인하면 작업 상태가 `completed`로 전이됩니다. 두 확인은 같은 범위 버전과 같은 증거 집합을 대상으로 해야 하며, 대기 중인 변경요청이나 잠기지 않은 범위가 있으면 완료할 수 없습니다. 완료 증거는 첫 확인부터 검증된 객체 generation과 보존정책이 필요하며, 최종 확인은 같은 transaction에서 객체 snapshot과 보존기간 뒤의 삭제 작업을 기록합니다. 실제 dispatch와 물리 삭제는 B runtime 연결 뒤 수행됩니다. 주요 권한·범위·변경·완료 사실은 비밀값과 자유서술 원문을 제외한 append-only 감사 이력으로 조회할 수 있습니다.

범위 잠금, 현장 변경요청, 완료 미디어 등록은 업무 트랜잭션 안에서 Outbox event를 저장합니다. `python -m app.entrypoints.outbox_relay`를 한 번 실행하면 due event를 lease로 선점해 Pub/Sub에 발행하며, 실패 event는 지수 backoff로 다시 시도됩니다. 소비자는 `event_id`별 영속 receipt로 중복 효과를 막고, 참여자별 알림 intent에는 연락처나 메시지 원문 없이 발송 상태와 정제된 오류 코드만 저장합니다.

notification consumer는 worker가 수신한 `DomainEvent`를 `consume_notification_event` application command에 전달합니다. 실제 연락처 해석과 이메일·메시지 provider 호출은 이 저장소의 intent 상태 command 바깥에서 수행하며, 성공·실패 결과만 `mark_notification_sent`, `mark_notification_failed`로 반영합니다.

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
| `SEQRET_OUTBOX_BATCH_SIZE` | `100` | relay 한 번에 lease하고 동시에 발행할 최대 event 수 |
| `SEQRET_OUTBOX_LEASE_SECONDS` | `60` | 발행 worker의 event lease 시간. publish timeout보다 길어야 함 |
| `SEQRET_EVENT_PUBLISH_TIMEOUT_SECONDS` | `10` | 개별 Pub/Sub 발행 완료를 기다리는 최대 시간 |
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
