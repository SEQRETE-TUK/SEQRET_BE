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
- [AI 작업 지침](AGENTS.md)

## 상태

`FND-A01` 기준 FastAPI application factory와 API·worker·job이 공유하는 설정 계약을 구성하는 단계입니다.

## 로컬 개발

Python 버전과 dependency는 `uv`로 고정합니다.

```bash
uv sync --dev
uv run uvicorn app.entrypoints.api:app --reload
```

기본 설정은 `SEQRET_` 접두사의 환경변수로 덮어쓸 수 있습니다. 로컬 설정은 `.env.example`을 참고하되 실제 `.env` 파일과 비밀값은 commit하지 않습니다.

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SEQRET_APP_NAME` | `SEQRET Backend` | API 문서와 application에 표시할 이름 |
| `SEQRET_SERVICE_NAME` | `seqret` | 소문자 DNS label 형식의 runtime 공통 식별자 |
| `SEQRET_ENVIRONMENT` | `local` | `local`, `test`, `staging`, `production` 중 하나 |
| `SEQRET_DEBUG` | `false` | FastAPI debug 여부. production에서는 `true`를 거부함 |
| `SEQRET_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` 중 하나 |
| `SEQRET_API_PREFIX` | `/api/v1` | 향후 업무 API가 사용할 정규화된 절대 경로 접두사 |

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

API가 정상적으로 bootstrap됐는지는 `GET /healthz`로 확인합니다.

- health 응답은 중간 cache가 이전 상태를 재사용하지 않도록 `Cache-Control: no-store`를 반환합니다.
- production 환경에서는 `/docs`, `/redoc`, `/openapi.json` HTTP endpoint를 노출하지 않습니다.
