# 팀 인계 — 현재 `main` 기준

> 작성일: 2026-08-13
> 기능 기준 커밋: `8180a66`
> 실행 계약의 단일 원본: 최신 `main` 코드와 비운영 환경의 `/openapi.json`

이 문서는 현재 구현과 팀 간 경계를 요약한다. 화면 초안이나 미래 API 제안은 실행 계약이 아니며, 제품 결정과 계약 PR을 거쳐 OpenAPI에 반영된 뒤 연동한다.

## 현재 제공 범위

- route는 26개다: `/api/v1` 업무 route 23개와 `/healthz`, `/readyz`, `/edgez` 3개다.
- 작업 bootstrap, 역할별 access link 인증·회전·철회, 촬영 세션과 미디어 업로드 완료 확인, 불변 작업범위와 양측 승인, 현장 변경요청과 증거 열람, 완료 확인, 감사·알림 조회, 미디어 보존 background job을 제공한다.
- Terraform은 예약 Outbox relay를 Cloud Scheduler가 매분 실행하도록 정의한다. lease 소유권을 잃은 미확정 event가 있으면 성공으로 숨기지 않고 실패 종료한다.
- `645313b`부터 `8180a66`까지의 A 변경은 응답 JSON 필드를 제거하거나 이름을 바꾸지 않았다. 촬영·access link·변경요청의 동시성, DB commit 시점과 signed URL 원문 보존을 강화했다.

## FE 연동 계약

- 업무 prefix는 `/api/v1`이고, 인증은 `Authorization: Bearer <access-link-secret>`이다.
- 역할값은 `customer`, `company_manager`, `field_worker`다. access link는 개인 신원을 증명하는 로그인 계정이 아니라 한 작업과 역할에 묶인 capability다.
- `POST /move-jobs`는 공개 bootstrap route다. 정확히 세 역할의 capability secret을 한 번만 반환하며, 호출자는 이를 각 참여자에게 전달할 신뢰 주체여야 한다. 신뢰 bootstrap과 전달 채널이 정해지기 전에는 일반 FE 연동 대상으로 취급하지 않는다.
- 다른 작업의 resource는 `404`, 역할 부족은 `403`, 유효한 access link의 제한 초과는 `429`와 `Retry-After`로 응답한다. 현재 오류 body는 FastAPI의 `detail` 기반이며 별도 machine error code는 계약하지 않았다.
- upload/read signed URL은 **opaque 문자열**이다. decode, 재직렬화, query 정렬, host 소문자화, 기본 port 제거를 하지 말고 받은 문자열을 그대로 사용한다. upload 응답의 `upload_headers`도 key·value를 정규화하지 않고 모두 PUT에 적용한다. GCS target에는 요청 MIME type의 `Content-Type`과 `x-goog-if-generation-match: 0`이 포함돼야 하며 어느 쪽도 빼면 안 된다.
- secret 또는 signed URL을 담는 응답은 `Cache-Control: no-store`다. PWA cache, 브라우저 영구 저장소, 로그와 analytics에 남기지 않는다.
- upload 완료 요청에 object generation을 보내지 않는다. FE는 발급받은 URL과 `upload_headers`로 PUT하고, BE가 storage metadata의 object key, MIME type, 크기와 generation을 검증한다.
- production은 `/docs`, `/redoc`, `/openapi.json`을 노출하지 않는다. client schema는 검증된 `main`으로 비운영 환경에서 생성한다.

## 현재 연동 blocker

- BE는 배포 환경의 `FRONTEND_ORIGIN` 하나만 API CORS로 허용한다. Vercel에서 직접 호출하기 전에 실제 canonical HTTPS origin을 설정하며 wildcard, port와 path는 허용하지 않는다.
- GCS upload에는 API CORS와 별개의 bucket CORS가 필요하다. 실제 upload method·필수 header와 bucket 정책은 B의 Storage adapter가 병합된 뒤 함께 검증한다.
- 현재 `main`은 production `StoragePort`를 wiring하지 않으므로 media upload/read URL route는 배포 환경에서 `503`을 반환한다. 관련 B PR이 병합되기 전에는 실서버 미디어 연동 완료로 간주하지 않는다.
- [SEQRET_FE](https://github.com/SEQRETE-TUK/SEQRET_FE)는 현재 Next.js 16·React 19 API 미연동 UI 데모이며 API client, CI와 GitHub 배포 증적이 없다. `docs/TECH_STACK.md`의 React·Vite·TanStack 기준과 실제 FE 저장소가 다르므로 팀 결정 없이 한쪽을 임의로 맞추지 않는다.

## B 트랙 인계

2026-08-13 기준 열린 B PR은 다음과 같다. A ORM과 repository를 B에서 직접 갱신하지 않고, 병합된 `app/contracts` Port와 공개 application command를 사용한다.

| PR | 범위 | 병합 전 확인 |
| --- | --- | --- |
| [#37](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/37) | GCP SDK 의존성 | 최신 `main` rebase와 full CI 후 B adapter보다 먼저 병합 |
| [#39](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/39) | GCS `StoragePort` adapter | #37과 FND-A03 계약 보강 병합 후 최신 `main`으로 rebase하고 upload target·generation delete 계약 및 full CI 재검증 |
| [#35](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/35) | AI 분석 실행·초안 저장 | 최신 `main` rebase, 단일 Alembic head와 full CI 확인 후 A 승인 |
| [#36](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/36) | 미디어 삭제 handler | 최신 `main` rebase와 full CI 후 독립 병합 가능; runtime 활성화 전 B-01/B-02 배선 확인 |

Outbox 전달은 at-least-once다. consumer는 `(consumer_name, event_id)` receipt로 중복 효과를 막아야 한다. 저장된 object generation은 read/delete까지 그대로 전달하며, B handler가 A 상태를 우회 갱신하면 안 된다. 공용 `StorageUploadTarget`은 provider header를 해석하지 않으므로, B의 GCS adapter가 `Content-Type`과 `x-goog-if-generation-match: 0`을 V4 서명에 포함하고 같은 값을 target으로 반환하는 adapter contract test를 통과한 뒤 병합한다.

## staging 운영 증적

- [Deploy staging #31636130577](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31636130577): migration, Terraform apply, 매분 예약 Outbox relay 실행, readiness, canary와 promotion 성공 (`329d386`).
- [Rollback #31630162261](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31630162261): 기록된 1회 rollback target으로 traffic 복구와 tag 소비 성공.
- [Roll-forward #31630440137](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31630440137): 최신 revision 재배포와 promotion 성공.
- [PITR recovery #31633614222](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31633614222): 복구 구간 확인, clone, Alembic head·marker 검증과 clone 삭제 성공.

위 실행으로 deploy·rollback·PITR 절차는 검증했다. 다만 `645313b`부터 `8180a66`까지의 최신 A correctness 변경은 위 deploy 이후 병합됐으므로, staging의 현재 image와 동일하다고 간주하지 않는다. 다음 배포는 최신 `main`에서 실행하고 workflow summary의 source SHA와 image digest를 증거로 남긴다.

상세 운영 절차와 외부 GCP·GitHub 변수는 [`infrastructure/README.md`](../infrastructure/README.md)를 따른다.
