# 팀 인계 — 현재 `main` 기준

> 작성일: 2026-08-15
> 기능 기준 커밋: 이 문서를 포함한 최신 `main`
> 실행 계약의 단일 원본: 최신 `main` 코드와 비운영 환경의 `/openapi.json`

이 문서는 현재 구현과 팀 간 경계를 요약한다. 화면 초안이나 미래 API 제안은 실행 계약이 아니며, 제품 결정과 계약 PR을 거쳐 OpenAPI에 반영된 뒤 연동한다.

## 현재 제공 범위

- OpenAPI operation은 26개 path에 31개다: `/api/v1` 업무 operation 28개와 `/healthz`, `/readyz`, `/edgez` 3개다.
- 작업 bootstrap, 역할별 access link 인증·회전·철회, 본인 촬영 세션·미디어 상태 복구, 미디어 업로드·분석 제출과 상태 조회, 고객 AI 초안 검토·완료, 불변 작업범위와 양측 승인, 현장 변경요청과 증거 열람, 완료 확인, 감사·알림 조회, 미디어 보존 background job을 제공한다.
- Terraform은 예약 Outbox relay를 Cloud Scheduler가 매분 실행하도록 정의한다. 이 Job은 Outbox·알림 pump와 due 미디어·분석 Cloud Task dispatch를 함께 수행한다. lease 소유권을 잃은 미확정 작업이 있으면 실패 종료하고, batch limit에 반복 도달하면 경보한다.
- 최신 `main`에는 Redis Direct VPC 선택 경로, GCS adapter, AI 분석 실행·초안 저장, 미디어 validation·삭제 handler와 Cloud Tasks private worker가 병합됐다. B 모듈은 공개 HTTP route를 추가하지 않는다.
- Alembic은 단일 head `int_01_0001`이다. 감사·완료 확인·촬영 분석·Outbox·알림·소비 이력이 생긴 환경에서는 schema downgrade가 차단되므로 rollback은 확장 schema를 유지한 application revision 전환으로 수행한다.

## FE 연동 계약

- 업무 prefix는 `/api/v1`이고, 인증은 `Authorization: Bearer <access-link-secret>`이다.
- 역할값은 `customer`, `company_manager`, `field_worker`다. access link는 개인 신원을 증명하는 로그인 계정이 아니라 한 작업과 역할에 묶인 capability다.
- `POST /move-jobs`는 공개 bootstrap route다. 정확히 세 역할의 capability secret을 한 번만 반환하며, 호출자는 이를 각 참여자에게 전달할 신뢰 주체여야 한다. 신뢰 bootstrap과 전달 채널이 정해지기 전에는 일반 FE 연동 대상으로 취급하지 않는다.
- 다른 작업의 resource는 `404`, 역할 부족은 `403`, 유효한 access link의 제한 초과는 `429`와 `Retry-After`로 응답한다. 현재 오류 body는 FastAPI의 `detail` 기반이며 별도 machine error code는 계약하지 않았다.
- 유효한 access link가 인증에 처음 사용되면 `participant_connected` 감사 event가 한 번 기록된다. payload에는 link·participant 식별자와 역할만 포함되며 bearer secret은 포함하지 않는다.
- upload/read signed URL은 **opaque 문자열**이다. decode, 재직렬화, query 정렬, host 소문자화, 기본 port 제거를 하지 말고 받은 문자열을 그대로 사용한다. upload 응답의 `upload_headers`도 key·value를 정규화하지 않고 모두 PUT에 적용한다. GCS target에는 요청 MIME type의 `Content-Type`과 `x-goog-if-generation-match: 0`이 포함돼야 하며 어느 쪽도 빼면 안 된다.
- secret 또는 signed URL을 담는 응답은 `Cache-Control: no-store`다. PWA cache, 브라우저 영구 저장소, 로그와 analytics에 남기지 않는다.
- upload 완료 요청에 object generation을 보내지 않는다. FE는 발급받은 URL과 `upload_headers`로 PUT하고, BE가 storage metadata의 object key, MIME type, 크기와 generation을 검증한다.
- `GET /move-jobs/{job_id}/capture-sessions`는 호출자가 만든 세션만 최신순으로 반환한다. 각 세션의 `media_assets`에서 validation 상태를 복구하고 선택적 `analysis`에서 분석 상태를 이어간다. object key, generation, signed URL과 provider task ID는 이 응답에 없다.
- 촬영 소유자는 모든 inventory 미디어가 비동기 validation을 마쳐 `READY`가 된 뒤 `POST /move-jobs/{job_id}/capture-sessions/{capture_session_id}/submit`을 한 번 호출한다. `202` 응답의 `analysis_run_id`를 보관하고 `GET .../analysis`로 상태를 조회할 수 있다. 제출 뒤 해당 촬영 세션의 새 upload·미완료 upload 확정은 `409`다.
- 분석 상태는 `pending`, `dispatching`, `queued`, `running`, `completed`, `failed`다. `completed`이면 `scope_version_id`, `failed`이면 provider-neutral `failure_code`와 `retryable`을 사용한다. provider task ID·object key·원본 오류는 FE 계약이 아니다.
- `GET /move-jobs/{job_id}/analysis-review`는 고객이 가장 최근에 제출한 분석이 `completed`일 때만 공간별 전체·READY·FAILED media 수와 현재 편집 항목을 반환한다. AI 항목에는 신뢰도·검토 필요·source media ID를 제공하지만 model·prompt·provider 정보는 노출하지 않는다.
- `POST /move-jobs/{job_id}/analysis-review/complete`는 응답의 `source_scope_version_id`와 최종 항목 전체를 받아 고객 편집본을 불변 자식 scope version으로 생성한다. 같은 항목의 재전송은 같은 결과를 반환하고 stale 원본, 다른 내용의 재전송, 다른 참여자가 만든 자식 version은 `409`다. 현재 v1은 항목 key·공간·설명을 지원하며 수량·단위·작업 메모는 AI schema v2 범위다.
- production은 `/docs`, `/redoc`, `/openapi.json`을 노출하지 않는다. client schema는 검증된 `main`으로 비운영 환경에서 생성한다.

## FE 현재 상태와 blocker

- BE는 배포 환경의 `FRONTEND_ORIGIN` 하나만 API CORS로 허용한다. Vercel에서 직접 호출하기 전에 실제 canonical HTTPS origin을 설정하며 wildcard, port와 path는 허용하지 않는다.
- staging은 FE 도메인 미구매 상태라 `https://34-160-87-130.sslip.io`를 임시 allowlist로 사용한다. 이는 공개 API edge 확인용이지 canonical FE origin 증적이 아니며, FE 배포 후 GitHub environment 변수와 bucket CORS를 함께 교체한다.
- GCS upload에는 API CORS와 별개의 bucket CORS가 필요하다. 실제 FE origin과 `PUT`, `Content-Type`, `x-goog-if-generation-match`를 허용한 뒤 브라우저 preflight와 create-only upload를 함께 검증한다.
- 배포 API는 `MEDIA_BUCKET_NAME`으로 지정한 private bucket과 API service account signer를 `StoragePort`에 연결한다. 실제 bucket CORS와 외부 IAM 선행조건은 별도로 검증한다.
- canonical [SEQRET_FE](https://github.com/SEQRETE-TUK/SEQRET_FE)의 `main`은 `16b4a98b812e798ad62942f0d82d5d6d7e715068`다. Vite 7·React 19 기반이며, FE PRD가 소비자 12개·업체 mobile 6개·업체 web 4개·작업자 5개인 총 27개 시각 demo 화면을 선언한다.
- FE [#2](https://github.com/SEQRETE-TUK/SEQRET_FE/pull/2)에서 API client와 Query 기반을 마련했고, FE [#4](https://github.com/SEQRETE-TUK/SEQRET_FE/pull/4)는 `/consumer/capture`에서 작업·세션 조회, 세션 생성, opaque signed PUT, upload 완료, READY 확인과 분석 제출·terminal polling을 연결했다. FE [#5](https://github.com/SEQRETE-TUK/SEQRET_FE/pull/5)는 AI 초안 조회·편집·완료를 연결했고, FE [#6](https://github.com/SEQRETE-TUK/SEQRET_FE/pull/6)은 미확정 이탈 방지와 `409` 최신 상태 복구를 추가했다. secret은 React 메모리에만 보관하며 390x844 mock browser E2E에서 정상 확정, 충돌 복구, 콘솔·영구 저장소·DOM secret 노출 여부를 검증했다. 나머지 시각 demo 흐름은 여전히 local state와 timer이므로 API 완료 증거로 보지 않는다.
- FE [#7](https://github.com/SEQRETE-TUK/SEQRET_FE/pull/7)은 화면 단위 dynamic import로 초기 JS를 224.60 kB까지 줄이고 500 kB 경고를 제거했다. production preview에서 7개 진입 경로와 전용 chunk 로드를 검증했다.
- FE 작업 지침은 실제 Vite source에 맞는 `AGENTS.md`로 정정됐다. access secret은 호출 시 메모리에서만 전달하고, signed URL·header와 함께 영구 저장소·로그에 남기지 않는 규칙을 포함한다.
- FE [#2](https://github.com/SEQRETE-TUK/SEQRET_FE/pull/2)부터 [#7](https://github.com/SEQRETE-TUK/SEQRET_FE/pull/7)까지 필수 CI는 성공했다. GitHub deployment와 environment는 2026-08-15 재확인 기준 각각 0개이며 canonical HTTPS origin도 없으므로 FE가 배포됐다고 간주하거나 API·GCS CORS를 실제 origin으로 교체하지 않는다.

## B 트랙 인계

- 병합 완료: GCS SDK [#37](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/37), B-01 GCS adapter [#39](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/39), B-03 분석 실행 [#35](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/35), B-04 Vertex adapter [#79](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/79), B-06 worker [#81](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/81)·[#82](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/82)·[#84](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/84), B-07 삭제 handler [#36](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/36).
- upload 완료는 generation-pinned validation intent를 만들고 B-05 handler가 metadata·MIME type·크기·SHA-256을 검증해 `PROCESSING → READY|FAILED` command로 반영한다. INT-01은 검증된 MIME 계약 [#85](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/85)·[#86](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/86), event 계약 [#87](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/87)과 현재 촬영 제출·durable dispatch·범위 초안 import 구현으로 연결됐다. 남은 B 범위는 INT-06 provider 재시도·동시 실행 recovery와 승인된 파생 처리 정책이다.
- Outbox 전달은 at-least-once다. consumer는 `(consumer_name, event_id)` receipt로 중복 효과를 막고, B handler는 A ORM을 우회 갱신하지 않는다. 저장된 object generation은 read/delete까지 그대로 전달한다.

## 외부 활성화 전 확인

- **GCS:** staging private bucket `seqret-stg-20260813-media`, `MEDIA_BUCKET_NAME`, runtime IAM과 임시 origin CORS는 준비됐다. 실제 FE origin을 확정하면 임시 origin을 교체하고 브라우저 preflight와 create-only upload를 다시 검증한다.
- **Redis:** `REDIS_URL_SECRET_ID`, `REDIS_VPC_NETWORK`, `REDIS_VPC_SUBNETWORK`을 함께 설정한다. 같은 region의 기존 `/26` 이상 subnet, Memorystore authorized network와 Cloud Run service-agent network 권한을 확인하고, 배포 후 Memorystore metric으로 실제 연결을 증명한다.
- **DB 역할:** API, migration, Outbox relay와 recovery가 현재 같은 `DATABASE_URL_SECRET_ID`를 사용한다. `audit_event` mutation trigger는 이 owner의 일반 DML도 거부하지만 owner는 DDL로 우회할 수 있으므로 tamper-proof 권한 경계로 간주하지 않는다. 별도 DB 사용자·grant·Secret ID와 자격증명이 외부에서 준비되기 전에는 Terraform secret만 나누지 않는다.
- **DB 연결 경보:** Cloud SQL `num_backends` 임계치는 승인된 instance `max_connections`와 connection budget이 정해진 뒤 추가한다. 임의 임계치로 경보를 만들지 않는다.
- **Artifact Registry:** 90일 초과 삭제 후보와 최신 50개 보존 정책은 계속 dry-run이다. provider cleanup audit log를 검토한 뒤에만 실제 삭제로 전환한다.
- **B runtime:** Cloud Tasks queue·OIDC private worker 배포와 validation 실경로는 staging에서 확인했다. B-04 코드와 SDK는 병합됐으며 실제 Vertex 분석에는 staging runtime IAM·location·model 접근을 검증해야 한다.
- **INT-01 runtime:** 현재 변경은 로컬·CI PostgreSQL에서 제출·dispatch·worker 결과 import를 검증했지만 staging에는 아직 배포하지 않았다. 병합 뒤 Vertex IAM·model 설정을 확인하고 실제 분석 1건의 `completed|failed` terminal 상태와 Outbox를 증적으로 남긴다.

## staging 운영 증적

- [Deploy staging #31811161952](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31811161952): 최신 application·migration 기준 `ecb325e`에서 migration, Terraform apply, 예약 Outbox relay, readiness, 10% canary와 promotion 성공. API `seqret-stg-api-00008-4b6`과 private worker `seqret-stg-worker-00001-xdf`가 동일 image digest `sha256:5cbf5c4ec7cc6d4fb27f7adc8e64d1b086cface8dac2e6cff65bafc023ee1f9a`를 사용한다.
- [Notification replay #31812191122](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31812191122): `ecb325e`에서 31일 retained-event seek와 relay smoke 성공. subscription label은 `replay_contract=v1`, `seqret_replay_state=complete`다.
- 2026-08-15 staging worker canary: API bootstrap → signed GCS PUT → upload complete → Outbox relay → Cloud Tasks → private worker validation을 실행했다. move job `fde893e4-d934-435e-b7ec-950763d39bc4`의 background job `31458876-8262-437a-9c7d-a24220a06548`이 1회 시도에 `succeeded`했고, 대상은 68-byte `image/png`였다. access secret과 signed URL은 저장하거나 출력하지 않았다.
- [PITR recovery #31816400843](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31816400843): `72c4840`에서 복구 구간 확인, point-in-time clone, read-only 연결, 현재 Alembic head·marker 검증과 exact clone 삭제 성공. 종료 후 `seqret-stg-recovery-*` 인스턴스는 0개다.
- [PR #76](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/76): 첫 최신-main 복구 drill에서 발견한 proxy readiness loop의 미정의 변수를 수정했다. 실패 실행도 recovery clone 삭제에는 성공했고, 수정 후 위 PITR drill로 회귀 검증했다.

이전 rollback 절차의 증적은 다음과 같다.

- [Deploy staging #31636130577](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31636130577): migration, Terraform apply, 매분 예약 Outbox relay 실행, readiness, canary와 promotion 성공 (`329d386`).
- [Rollback #31630162261](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31630162261): 기록된 1회 rollback target으로 traffic 복구와 tag 소비 성공.
- [Roll-forward #31630440137](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31630440137): 최신 revision 재배포와 promotion 성공.
- [PITR recovery #31633614222](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31633614222): 복구 구간 확인, clone, Alembic head·marker 검증과 clone 삭제 성공.

현재 staging application·schema는 `ecb325e` 배포 증적과 일치한다. `72c4840`은 recovery workflow만 수정했으며 이 SHA에서 PITR까지 재검증했다. 이후 application, migration 또는 Terraform 변경이 병합되면 다시 배포하고 workflow summary의 source SHA와 image digest를 새 증거로 남긴다.

상세 운영 절차와 외부 GCP·GitHub 변수는 [`infrastructure/README.md`](../infrastructure/README.md)를 따른다.
