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
- [AI 작업 지침](AGENTS.md)

## 상태

FastAPI·PostgreSQL 기반, 두 트랙의 공통 계약과 작업·참여자·위치·구역 API를 구성했습니다.

업무 API는 설정된 API prefix(기본 `/api/v1`) 아래에서 제공됩니다.

- `POST /move-jobs`: 작업과 초기 참여자·위치·구역 생성
- `GET /move-jobs/{job_id}`: 전체 작업 구성 조회
- `POST /move-jobs/{job_id}/participants`: 작업 역할 참여자 연결
- `POST /move-jobs/{job_id}/participants/{participant_id}/access-links`: 역할 링크 재발급
- `POST /move-jobs/{job_id}/access-links/{access_link_id}/revoke`: 역할 링크 철회
- `POST /move-jobs/{job_id}/capture-sessions`: 참여자 소유 촬영 세션 생성
- `POST /move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/upload`: 업로드 URL 발급
- `POST /move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/{media_asset_id}/complete`: 업로드 메타데이터 확인
- `POST /move-jobs/{job_id}/scope-versions`: 불변 작업범위 버전 생성
- `GET /move-jobs/{job_id}/scope-versions`: 작업범위 버전 이력 조회
- `POST /move-jobs/{job_id}/scope-versions/{scope_version_id}/approvals`: 작업범위 버전 확인

위치에는 주소 원문 대신 화면 표시용 label만 저장합니다.

작업 생성과 참여자 연결 응답은 7일 동안 유효한 역할 링크의 비밀값을 한 번만 반환합니다. 이후 작업 API는 이 값을 `Authorization: Bearer <secret>`으로 받으며 데이터베이스에는 SHA-256 hash만 저장합니다. 고객과 회사 관리자는 참여자를 연결할 수 있고, 현장 작업자는 작업 조회만 할 수 있습니다. 링크를 재발급하면 기존 링크는 즉시 철회됩니다.

촬영 미디어는 작업에 속한 구역과 `inventory` 또는 `condition` 목적으로만 최초 등록할 수 있습니다. 사진은 20 MiB, 영상은 200 MiB로 제한하며, 업로드 완료 시 `StoragePort`로 MIME type과 정확한 크기를 다시 확인합니다. 비공개 객체의 signed URL은 응답으로만 전달하고 데이터베이스와 로그에는 저장하지 않습니다. 실제 스토리지 adapter가 구성되지 않은 환경에서는 업로드 API가 `503`을 반환합니다.

작업범위 편집은 기존 row를 덮어쓰지 않고 현재 버전을 부모로 삼는 새 snapshot을 생성합니다. 각 버전은 작업별 순번과 canonical JSON의 SHA-256 content hash를 가지며, 한 부모에서 두 갈래 버전이 생기지 않도록 데이터베이스 제약으로 선형 이력을 유지합니다.

고객과 회사 관리자가 같은 현재 버전을 각각 확인하면 해당 버전이 잠깁니다. 이미 다음 버전이 있는 과거 버전, 중복 확인, 잠긴 버전의 후속 편집은 거부합니다.

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

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

DB migration과 테스트 DB 운영 방법은 [데이터베이스 개발 가이드](docs/DATABASE.md)를 따릅니다.

API가 정상적으로 bootstrap됐는지는 `GET /healthz`로 확인합니다.

- health 응답은 중간 cache가 이전 상태를 재사용하지 않도록 `Cache-Control: no-store`를 반환합니다.
- production 환경에서는 `/docs`, `/redoc`, `/openapi.json` HTTP endpoint를 노출하지 않습니다.
