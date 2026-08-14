# SEQRET MVP API 구현 현황

> 기준일: 2026-08-15
>
> backend 기준 코드: `origin/main` `a59dac44daf5c388d3c8c91db30cae26752e068a` + 이 A-03 변경
>
> frontend 확인 기준: `SEQRETE-TUK/SEQRET_FE` `origin/main`
> `aabf2da2221d63d4debc5f06b4d40e92f061289a`
>
> 관련 문서: [API 명세](API_SPEC.md), [추가 화면 요청서](FRONTEND_SCREEN_REQUEST.md)
>
> 실행 계약의 단일 원본: 최신 `main` 코드와 비운영 환경의 `/openapi.json`

## 1. 한눈에 보기

| 구분 | 수량 | 의미 |
| --- | ---: | --- |
| 현재 FastAPI 등록 operation | 29개 | 24개 path의 업무 operation 26개 + 운영 operation 3개 |
| 최신 FE가 선언한 시각 demo 화면 | 27개 | 소비자 12 + 업체 mobile 6 + 업체 web 4 + 작업자 5; API E2E 증거 아님 |
| FE 화면의 실제 API 호출 | 0개 | 공통 client·Query provider는 있으나 어떤 demo 화면도 호출하지 않음 |
| 기존 8화면 기준 backend 제안 API | 17개 | 현재 OpenAPI가 아닌 목표 계약 초안 |
| 추가 P0 화면 포함 시 backend 제안 API | 19개 | 작업 완료 제출과 고객 완료 결정 2개 추가 |
| 기존 핵심 logic을 재사용할 제안 API | 11개 | 19개 제안 기준 route 전환 2개 + 기능·응답 확장 9개 |
| 핵심 업무 상태부터 새로 만들 제안 API | 8개 | 19개 제안 기준 배차, 체크인, 완료 제출·요청, 문서 등 |

현재 route가 많다고 frontend 준비가 끝난 것은 아니다. frontend는 현재 26개 업무 operation과
[API 명세](API_SPEC.md), FE PRD 부록의 제안 경로를 혼용하지 않는다. 제품 범위와 A 소유 계약을
고정하고 OpenAPI에 구현한 경로만 연동한다. B 소유 Port·event·AI 결과 schema가 변하는 경우에만
해당 영향 범위를 별도로 조정한다.

## 2. 상태 기준

| 상태 | 판단 기준 |
| --- | --- |
| `전환` | 핵심 application service가 있어 최종 route와 schema adapter가 주된 작업 |
| `확장` | 관련 model·service는 있으나 화면용 필드, 상태 전이 또는 권한 계약 추가 필요 |
| `신규` | 핵심 업무 record나 service가 아직 없음 |
| `조건부` | 제품 운영 방식이 확정될 때만 추가 |

## 3. 최신 FE 대조

### 3.1 구현 형태

| 항목 | 확인 결과 | backend 판단 |
| --- | --- | --- |
| runtime | Vite 7, React 19, TypeScript | `TECH_STACK.md`의 Vite 선택과 일치 |
| 사용자 경로 | `/`, `/provider`, `/provider/web`, `/crew`, `/design-system` | 역할별 UI demo 경로만 존재 |
| 화면·상태 | PRD 기준 27개 화면과 `screen`, `view`, `state` query variant | 시각·상호작용 검증 자료로 사용; API 완료로 보지 않음 |
| server state | TanStack Query provider·공통 retry 정책 존재; 화면은 `useState`, `setTimeout` | 기반은 준비됐지만 새로고침·동시 사용자·server 정합성 E2E는 아직 없음 |
| API 기반 | `VITE_API_BASE_URL`, `/api/v1` 제한, 명시적 Bearer, opaque signed PUT client 존재 | 승인된 첫 OpenAPI slice를 화면 query·mutation에 연결할 단계 |
| CI·배포 | Actions 4회 성공; deployment와 environment 0개 | code quality gate는 존재하지만 canonical HTTPS origin·실배포는 없음 |

### 3.2 계약 불일치

| 항목 | 최신 FE PRD 부록 | 현재 backend 실행 계약 |
| --- | --- | --- |
| base path | `https://api.{service}.kr/v1` | `/api/v1` |
| 역할 | `consumer`, `provider`, `crew` | `customer`, `company_manager`, `field_worker` |
| 오류 body | `{error: {code, message, request_id}}` | FastAPI `detail`; 일부 응답에 `x-request-id` |
| 업무 경로 | `/jobs`, `/change-orders`, `/assignment`, `/completion/*` 등 | `/move-jobs`, `/capture-sessions/*/submit`, `/change-requests`, `/scope-versions` 등 26개 operation |
| upload 완료 | `/media` 또는 `/completion/media` 한 단계처럼 기술 | URL 발급 후 opaque headers PUT, 별도 complete command와 비동기 validation |

FE PRD 부록은 화면 요구를 설명하는 대안 제안이며 OpenAPI나 구현 증거가 아니다. 특히 현재
generation-pinned upload, 불변 scope version, capability role과 감사 계약을 약화하는 방향으로
경로만 맞추지 않는다. 최신 FE 화면에서 실제 MVP slice를 정한 뒤 A가 한 계약씩 OpenAPI와 test로
확정한다.

## 4. frontend 목표 API 제안 현황

### 4.1 기존 와이어프레임 17개

| 화면 | Method · Path | 용도 | 상태 | 재사용 기반·남은 일 |
| --- | --- | --- | --- | --- |
| 고객·업체 범위 | `GET /api/v1/move-jobs/{job_id}/scope-review` | 최신 범위, 금액, 사진, 양측 확인 상태 조회 | 확장 | 작업·범위 version 조회 재사용; 화면 view와 금액 snapshot 추가 |
| 고객 범위 | `POST /api/v1/move-jobs/{job_id}/scope-review/revision-request` | 고객 수정 요청 접수 | 신규 | 사전 범위 수정요청 record와 상태 전이 필요 |
| 고객 범위 | `POST /api/v1/move-jobs/{job_id}/scope-review/confirm` | 고객의 현재 범위 확인 | 전환 | 기존 `approve_scope_version` 재사용; 고객 전용 계약으로 노출 |
| 업체 범위 | `POST /api/v1/move-jobs/{job_id}/scope-proposals` | 범위·금액 제안을 고객에게 전송 | 확장 | 범위 version 생성 재사용; 수량·작업·금액·제안 상태 추가 |
| 고객 AI 검토 | `GET /api/v1/move-jobs/{job_id}/analysis-review` | 업로드와 AI 검토 초안 조회 | 확장 | 저장된 분석 원본과 media 재사용; 화면 view와 AI schema v2 필요 |
| 고객 AI 검토 | `POST /api/v1/move-jobs/{job_id}/analysis-review/complete` | 고객 수정 결과를 업체 검토 초안으로 제출 | 확장 | `import_analysis_draft` 재사용; HTTP command와 수정된 항목 계약 필요 |
| 고객 변경 승인 | `GET /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}` | 현장 변경 사유, 증빙, 금액 조회 | 확장 | 변경요청 record 재사용; 금액과 signed preview 조합 필요 |
| 고객 변경 승인 | `POST /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/decision` | 승인·거절·설명 요청 | 확장 | 기존 결정·설명 상태 전이 재사용; 고객 권한과 제안 계약으로 정리 |
| 업체 배차 | `GET /api/v1/move-jobs/{job_id}/dispatch` | 차량·작업자 후보와 충돌 조회 | 신규 | 차량, 작업자 자원, 가용성, 배정 model·service 필요 |
| 업체 배차 | `PUT /api/v1/move-jobs/{job_id}/dispatch` | 배정 확정과 알림 생성 | 신규 | 원자적 충돌 검증, 배정 저장과 알림 연결 필요 |
| 업체 완료 | `GET /api/v1/move-jobs/{job_id}/completion-summary` | 완료 사진, 근무, 변경, 금액, 문서 요약 | 확장 | 완료 확인·감사 기반 재사용; 작업자 제출·체크리스트·금액·문서 data 추가 |
| 업체 완료 | `POST /api/v1/move-jobs/{job_id}/completion-requests` | 고객에게 완료 확인 요청 | 신규 | 요청 lifecycle과 deep-link 알림 trigger 필요 |
| 업체 완료 | `GET /api/v1/move-jobs/{job_id}/documents/archive` | 증빙 PDF ZIP 다운로드 | 신규 | 문서 생성 상태와 archive 생성 service 필요 |
| 현장기사 범위 | `GET /api/v1/move-jobs/{job_id}/field-brief` | 최신 범위, 경로, 일정, 담당자와 현장 조건 조회 | 신규 | 기존 작업·범위에 배정·체크인 data를 합치는 view 필요 |
| 현장기사 범위 | `POST /api/v1/move-jobs/{job_id}/check-ins` | 현장 도착 시각 기록 | 신규 | 배정된 기사 검증과 check-in record 필요 |
| 현장기사 이슈 | `POST /api/v1/move-jobs/{job_id}/media-uploads` | 이슈 증빙 signed upload URL 발급 | 전환 | 기존 capture 3단계와 `StoragePort` 재사용; frontend path 단순화 |
| 현장기사 이슈 | `POST /api/v1/move-jobs/{job_id}/field-issues` | 범위 밖 작업·파손 위험·현장 장애 보고 | 확장 | 기존 변경요청·증빙 검증 재사용; 무가격 이슈와 업체 견적 단계를 분리 |

### 4.2 추가 P0 화면 승인 시 2개

| 화면 | Method · Path | 용도 | 상태 | 재사용 기반·남은 일 |
| --- | --- | --- | --- | --- |
| 현장기사 완료 기록 | `POST /api/v1/move-jobs/{job_id}/completion-submissions` | 완료 사진, 체크리스트, 실제 근무와 현장 확인 제출 | 신규 | 작업자 완료 제출 aggregate와 검증 필요 |
| 고객 완료 확인 | `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/decision` | 완료 확인 또는 문제 신고 | 확장 | 기존 완료 확인 logic 재사용; 요청 lifecycle과 문제 신고 상태 추가 |

두 API는 [추가 화면 요청서](FRONTEND_SCREEN_REQUEST.md)의 P0 화면이 승인되면 [API 명세](API_SPEC.md)에 편입한다. 승인 전 기준 API 수는 17개, 승인 후는 19개다.

### 4.3 조건부 API

| 조건 | 추가 API | 결정 기준 |
| --- | --- | --- |
| 고객·업체가 최초 사진을 직접 등록 | 화면용 `capture-submissions` adapter | 현재 저수준 capture 생성·upload·complete·submit·status 계약을 한 화면 command/view로 감쌀 때만 추가 |
| 업체가 작업과 고객 link를 직접 생성 | 작업 생성·초대 API | admin seed가 아닌 실제 업체 onboarding을 MVP에 넣을 때만 확정 |

## 5. 현재 서버 업무 operation 26개

이 표는 현재 호출 가능한 API의 용도와 최종 처리 방향이다. `유지`는 frontend 공개 유지를 뜻하지 않고 내부 bootstrap·운영 용도로 보존한다는 의미다.

| 현재 Method · Path | 현재 용도 | 최종 처리 |
| --- | --- | --- |
| `POST /api/v1/move-jobs` | 작업과 정확히 세 역할의 초기 capability 생성 | 신뢰 bootstrap·전달 채널 결정 전 일반 frontend 연동에서 제외 |
| `GET /api/v1/move-jobs/{job_id}` | 작업·참여자·공간 구성 조회 | 화면별 view에 필요한 header만 재사용 |
| `POST /api/v1/move-jobs/{job_id}/participants/{participant_id}/access-links` | 역할 link 재발급 | private bootstrap·운영으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/access-links/{access_link_id}/revoke` | 역할 link 철회 | private bootstrap·운영으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions` | 촬영 session 생성 | media upload 내부 logic으로 재사용 |
| `GET /api/v1/move-jobs/{job_id}/capture-sessions` | 본인 세션·미디어 validation·분석 상태 복구 | 첫 촬영·AI E2E slice의 query로 바로 사용 가능; signed capability와 provider 내부값은 제외 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/submit` | READY inventory 촬영을 동결하고 분석 intent 생성 | 첫 촬영·AI E2E slice의 command로 바로 사용 가능; 화면용 adapter 여부는 FE 연동 후 결정 |
| `GET /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/analysis` | 분석 queue·실행·범위 초안 terminal 상태 조회 | 첫 촬영·AI E2E slice의 polling query로 바로 사용 가능; review item view는 별도 필요 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/upload` | signed upload URL 발급 | `POST /media-uploads`로 축소 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/{media_asset_id}/complete` | 업로드 metadata 검증·확정 | 이슈·완료 제출 시 server 검증으로 흡수 |
| `POST /api/v1/move-jobs/{job_id}/scope-versions` | 불변 범위 version 생성 | `POST /scope-proposals` 내부 logic으로 재사용 |
| `GET /api/v1/move-jobs/{job_id}/scope-versions` | 범위 version 이력 조회 | `GET /scope-review` 화면 view로 교체 |
| `POST /api/v1/move-jobs/{job_id}/scope-versions/{scope_version_id}/approvals` | 고객·업체 범위 확인 | 고객 `POST /scope-review/confirm`으로 전환 |
| `POST /api/v1/move-jobs/{job_id}/change-requests` | 현장 변경요청 생성 | `field-issues`와 업체 `scope-proposals`로 단계 분리 |
| `GET /api/v1/move-jobs/{job_id}/change-requests` | 현장 변경요청 목록 조회 | 변경 상세와 완료 요약 view에 흡수 |
| `GET /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/evidence/{media_asset_id}/read-url` | READY 변경 증거의 generation-pinned 열람 URL 발급 | 제안 화면 view의 signed preview와 통합 여부 검토 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/clarification` | 현장기사에게 설명 요청 | 최종 decision action으로 통합 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/explanation` | 현장기사 설명 제출 | 별도 화면 승인 전 frontend에서 제외 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/decision` | 현장 변경 승인·거절 | `change-proposals/{id}/decision`에 재사용 |
| `POST /api/v1/move-jobs/{job_id}/completion-confirmations` | 고객·업체 완료 확인 | 고객 완료 결정과 업체 완료 요청 흐름으로 재구성 |
| `GET /api/v1/move-jobs/{job_id}/completion-confirmations` | 완료 확인 이력 조회 | `completion-summary`에 흡수 |
| `GET /api/v1/move-jobs/{job_id}/audit-events` | 전체 감사 이력 조회 | 내부 유지; 화면에는 필요한 요약만 제공 |
| `GET /api/v1/move-jobs/{job_id}/notifications` | 참여자 알림 이력 조회 | 알림함 화면이 없어 frontend에서 제외 |
| `POST /api/v1/move-jobs/{job_id}/background-jobs` | 보존 삭제 등 background job 생성 | 내부 운영 기능으로 유지 |
| `GET /api/v1/move-jobs/{job_id}/background-jobs` | background job 상태·오류 조회 | 내부 운영 기능으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/background-jobs/{background_job_id}/retry` | 실패·lease 만료 job 재실행 | 내부 운영 기능으로 유지 |

현재 upload 응답은 provider 값을 해석하지 않는 `upload_url`·`upload_headers`를 반환하고,
별도 complete command가 generation·MIME type·크기를 고정한 비동기 validation intent를 만든다.
제안 `POST /media-uploads`가 complete API를 흡수하려면 이 상태 전이와 재시도 계약을 먼저
versioned contract로 승인해야 한다. 현재 HTTP mutation 전체에 `Idempotency-Key` 공통 header는
없고 API CORS 허용 method도 `GET`, `POST`이므로 제안 계약을 문서만으로 frontend에 적용할 수 없다.
촬영 제출은 capture 소유자에게 멱등이며 제출 뒤 추가 media mutation을 막는다. 분석 상태 API는
`scope_version_id` 또는 provider-neutral 실패만 노출하므로, FE는 provider task ID나 오류 원문을
별도 계약으로 추정하지 않는다.

## 6. 운영 route 3개

| Method · Path | 용도 | frontend 사용 |
| --- | --- | --- |
| `GET /healthz` | process bootstrap 확인 | 사용하지 않음 |
| `GET /edgez` | 일반 public traffic 경로 확인 | 사용하지 않음 |
| `GET /readyz` | DB 연결 readiness 확인 | 사용하지 않음 |

## 7. 남은 backend 범위

### 계약·조회 재구성

- 화면 단위 view 6개: `scope-review`, `analysis-review`, `change-proposals/{id}`, `dispatch`, `completion-summary`, `field-brief`
- 공통 `JobHeader`, `ScopeLineV2`, `QuoteSnapshot`, signed `MediaPreview`
- AI `AnalysisResult` v2와 B consumer 영향 확인

### 신규 업무 상태

- 범위 수정요청
- 견적·금액 snapshot
- 배차·작업자·차량 가용성·체크인
- 작업자 완료 제출, 완료 확인 요청, 고객 결정·문제 신고
- 문서 생성 상태와 ZIP archive

### 예상 migration

INT-01은 실제 migration `int_01_0001`과 `capture_analysis_dispatch`로 제출·dispatch·terminal 상태를 추가했다. 배차·체크인, 견적, 완료 제출·요청·문제 신고와 문서 상태는 추가 persistence가 필요하므로 후속 migration 대상이다. 기존 범위 version, 승인, 변경요청, media, audit, notification table은 가능한 범위에서 재사용한다.

## 8. frontend 연동 기준

- 최신 main의 비운영 `/openapi.json`만 현재 실행 계약으로 사용한다.
- 현재 route 이름과 제안 경로를 섞어 임시 연동하지 않는다.
- 각 최종 endpoint가 OpenAPI에 추가되고 권한·오류·중복 호출 test가 통과한 뒤 연동한다.
- FE 공통 기반의 `VITE_API_BASE_URL`, API client와 TanStack Query 정책을 유지하고 capability secret은 화면에서도 메모리에만 보관한다.
- 화면 조회는 여러 CRUD 호출을 조합하지 않고 화면별 `GET` 한 번을 기본으로 한다.
- CTA 하나는 command endpoint 하나에 대응한다.
- 최신 FE 27개 중 첫 E2E slice와 추가 P0 화면 2개 포함 여부를 결정한 뒤 API 기준을 고정한다.
- FE canonical HTTPS origin이 생기기 전에는 staging 임시 origin과 GCS bucket CORS를 교체하지 않는다.
