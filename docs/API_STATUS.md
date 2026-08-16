# SEQRET MVP API 구현 현황

> 기준일: 2026-08-16
>
> backend 기능 기준 코드: 이 문서를 포함한 최신 `main`
>
> frontend 확인 기준: `SEQRETE-TUK/SEQRET_FE` `origin/main`
> `58ccfd0fa27353de7676f2251a96d0701f5a5aea`
>
> 관련 문서: [API 명세](API_SPEC.md), [추가 화면 요청서](FRONTEND_SCREEN_REQUEST.md)
>
> 실행 계약의 단일 원본: 최신 `main` 코드와 비운영 환경의 `/openapi.json`

## 1. 한눈에 보기

| 구분 | 수량 | 의미 |
| --- | ---: | --- |
| 현재 FastAPI 등록 operation | 60개 | 52개 path의 업무 operation 57개 + 운영 operation 3개 |
| 최신 FE가 선언한 시각 demo 화면 | 27개 | 소비자 12 + 업체 mobile 6 + 업체 web 4 + 작업자 5; API E2E 증거 아님 |
| FE 실제 API 연동 범위 | capture + 3역할 | 촬영·분석과 고객·업체·현장기사의 초대, 범위·변경, 배차·체크인, 완료 흐름을 query·mutation으로 호출; 별도 signed PUT 포함 |
| 기존 8화면 기준 backend 목표 API | 17개 | 16개 구현; 화면용 `media-uploads` adapter만 조건부 잔여 |
| 승인된 P0 화면 포함 backend 목표 API | 19개 | 18개 구현; 완료 제출과 고객 결정 포함 |
| 남은 목표 API | 1개 | 기존 capture 상태 전이를 보존할 upload adapter만 조건부 잔여 |

현재 route가 많다고 frontend 준비가 끝난 것은 아니다. frontend는 현재 57개 업무 operation과
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
| 사용자 경로 | `/`, `/consumer/capture`, `/provider`, `/provider/web`, `/crew`, `/design-system` | 역할별 업무 경로와 capture가 실제 API client를 사용하고 design system만 독립 시각 경로 |
| 화면·상태 | PRD 기준 27개 화면과 `screen`, `view`, `state` query variant | PRD 수치는 시각 범위이며 현재 역할별 runtime workflow 수와 동일하지 않음 |
| server state | TanStack Query 기반 역할별 query·mutation·polling, command replay와 `409` refetch | 초대 pending 동안 보호 API를 호출하지 않으며 수락 뒤 역할 업무 query를 활성화함 |
| API 기반 | `VITE_API_BASE_URL`, `/api/v1` 제한, 명시적 Bearer, opaque signed PUT client 존재 | capture·초대·scope·change·dispatch·completion 실행 계약을 화면에 연결함 |
| CI·배포 | FE #8·#9 포함 필수 CI 성공, Playwright 6개 통과; deployment와 environment 0개 | 자동 계약 검증은 있으나 canonical HTTPS origin·영구 배포는 없음 |

### 3.2 계약 불일치

| 항목 | 최신 FE PRD 부록 | 현재 backend 실행 계약 |
| --- | --- | --- |
| base path | `https://api.{service}.kr/v1` | `/api/v1` |
| 역할 | `consumer`, `provider`, `crew` | `customer`, `company_manager`, `field_worker` |
| 오류 body | `{error: {code, message, request_id}}` | FastAPI `detail`; 일부 응답에 `x-request-id` |
| 업무 경로 | `/jobs`, `/change-orders`, `/assignment`, `/completion/*` 등 | `/move-jobs`, `/invitations`, `/capture-sessions/*/submit`, `/analysis-review`, `/scope-review`, `/dispatch`, `/field-brief`, `/check-ins`, `/completion-*` 등 57개 operation |
| upload 완료 | `/media` 또는 `/completion/media` 한 단계처럼 기술 | URL 발급 후 opaque headers PUT, 별도 complete command와 비동기 validation |

FE PRD 부록은 화면 요구를 설명하는 대안 제안이며 OpenAPI나 구현 증거가 아니다. 특히 현재
generation-pinned upload, 불변 scope version, capability role과 감사 계약을 약화하는 방향으로
경로만 맞추지 않는다. 최신 FE 화면에서 실제 MVP slice를 정한 뒤 A가 한 계약씩 OpenAPI와 test로
확정한다.

## 4. frontend 목표 API 제안 현황

### 4.1 기존 와이어프레임 17개

| 화면 | Method · Path | 용도 | 상태 | 재사용 기반·남은 일 |
| --- | --- | --- | --- | --- |
| 고객·업체 범위 | `GET /api/v1/move-jobs/{job_id}/scope-review` | 최신 범위, 금액, 사진, 양측 확인 상태 조회 | 구현 | 현재 불변 범위·견적·수정요청·양측 확인과 generation-pinned preview를 한 view로 반환 |
| 고객 범위 | `POST /api/v1/move-jobs/{job_id}/scope-review/revision-request` | 고객 수정 요청 접수 | 구현 | 현재 고객 확인 대기 제안에 한해 immutable 요청을 생성하고 동일 요청은 재사용 |
| 고객 범위 | `POST /api/v1/move-jobs/{job_id}/scope-review/confirm` | 고객의 현재 범위 확인 | 구현 | 현재 업체 제안만 확인하며 기존 approval·scope lock·event를 원자적으로 재사용 |
| 업체 범위 | `POST /api/v1/move-jobs/{job_id}/scope-proposals` | 범위·금액 제안을 고객에게 전송 | 구현 | 범위 v1 snapshot, 원화 견적·포함·제외·사유를 저장하고 업체 확인을 함께 기록 |
| 고객 AI 검토 | `GET /api/v1/move-jobs/{job_id}/analysis-review` | 업로드와 AI 검토 초안 조회 | 구현 | 최신 고객 소유 완료 분석, 공간별 media 수와 AI provenance를 provider-neutral view로 반환 |
| 고객 AI 검토 | `POST /api/v1/move-jobs/{job_id}/analysis-review/complete` | 고객 수정 결과를 업체 검토 초안으로 제출 | 구현 | AI 원본을 기준으로 불변 고객 편집본을 생성하고 동일 payload 재전송은 멱등 처리 |
| 고객 변경 승인 | `GET /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}` | 현장 변경 사유, 증빙, 금액 조회 | 구현 | 변경요청·견적 snapshot과 generation-pinned READY preview를 한 view로 반환 |
| 고객 변경 승인 | `POST /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/decision` | 승인·거절·설명 요청 | 구현 | 고객 전용 결정, 정확 replay, 승인 시 양측 확인·scope lock을 원자적으로 수행 |
| 업체 배차 | `GET /api/v1/move-jobs/{job_id}/dispatch` | 차량·작업자 후보와 충돌 조회 | 구현 | 현재 범위에 묶인 작업별 immutable 후보 snapshot, 요구사항·충돌·선택 상태 반환 |
| 업체 배차 | `PUT /api/v1/move-jobs/{job_id}/dispatch` | 배정 확정과 알림 생성 | 구현 | 용량·인원·기술·자격·대표 기사를 원자 검증하고 `dispatch_confirmed.v1` 알림 연결 |
| 업체·고객 완료 | `GET /api/v1/move-jobs/{job_id}/completion-summary` | 완료 사진, 근무, 변경, 금액, 요청·문서 요약 | 구현 | 업체와 요청받은 고객의 단일 view; READY generation-pinned preview 포함 |
| 업체 완료 | `POST /api/v1/move-jobs/{job_id}/completion-requests` | 고객에게 7일 완료 확인 요청 | 구현 | 최신 제출·활성 요청·정확 replay 검증과 고객 알림 intent 연결 |
| 업체 완료 | `GET /api/v1/move-jobs/{job_id}/documents/archive` | 증빙 PDF 4종·manifest ZIP 다운로드 | 구현 | 결정적 archive; 준비 전 `409`, 완료 DB 사실과 생성 실패 분리 |
| 현장기사 범위 | `GET /api/v1/move-jobs/{job_id}/field-brief` | 최신 범위, 경로, 일정, 담당자와 현장 조건 조회 | 구현 | 확정 배정·현재 잠긴 범위·마스킹 위치·checklist와 체크인 상태를 한 view로 반환 |
| 현장기사 범위 | `POST /api/v1/move-jobs/{job_id}/check-ins` | 현장 도착 시각 기록 | 구현 | 배정된 대표 기사, 예정일 당일과 checklist 전체 확인을 검증하고 정확 replay 허용 |
| 현장기사 이슈 | `POST /api/v1/move-jobs/{job_id}/media-uploads` | 이슈 증빙 signed upload URL 발급 | 전환 | 기존 capture 3단계와 `StoragePort` 재사용; frontend path 단순화 |
| 현장기사 이슈 | `POST /api/v1/move-jobs/{job_id}/field-issues` | 범위 밖 작업·파손 위험·현장 장애 보고 | 구현 | 잠긴 범위와 기사 소유 UPLOADED·READY 증거를 검증하고 무가격 이슈와 업체 견적 단계를 분리; 제안 전에는 READY 필수 |

### 4.2 승인된 추가 P0 화면 2개

| 화면 | Method · Path | 용도 | 상태 | 재사용 기반·남은 일 |
| --- | --- | --- | --- | --- |
| 현장기사 완료 기록 | `POST /api/v1/move-jobs/{job_id}/completion-submissions` | 완료 사진, 체크리스트, 실제 근무와 현장 확인 제출 | 구현 | 체크인·현재 배차·범위·작업자·선택적 READY 미디어 검증과 정정 제출 지원 |
| 고객 완료 확인 | `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/decision` | 완료 확인 또는 문제 신고 | 구현 | 책임 자동판단 없이 문제를 분리하고 확인 시 완료·보존 intent를 원자 반영 |

두 P0 API는 승인되어 [API 명세](API_SPEC.md)와 OpenAPI에 편입됐다. 목표 19개 중 화면용 upload adapter를 제외한 18개가 구현됐다.

### 4.3 온보딩·조건부 API

| 조건 | 추가 API | 결정 기준 |
| --- | --- | --- |
| 고객·업체가 최초 사진을 직접 등록 | 화면용 `capture-submissions` adapter | 현재 저수준 capture 생성·upload·complete·submit·status 계약을 한 화면 command/view로 감쌀 때만 추가 |
| 소비자가 작업을 직접 생성 | `POST /api/v1/move-jobs/onboarding` | 구현. 고객 참여자와 고객 secret 하나만 발급 |
| 소비자가 업체를, 업체가 현장기사를 초대 | `/me`, invitation 생성·목록·수락·거절·폐기·재발급 | 구현. pending 업무 접근 차단과 상위 철회 cascade 포함 |

현장 변경 화면을 지원하기 위해 목표 17개 밖의 업체용 `GET /field-issues`,
`POST /change-proposals`, `POST /change-proposals/{id}/explanation`도 실행 계약으로 등록됐다.

## 5. 현재 서버 업무 operation 57개

이 표는 현재 호출 가능한 API의 용도와 최종 처리 방향이다. `유지`는 frontend 공개 유지를 뜻하지 않고 내부 bootstrap·운영 용도로 보존한다는 의미다.

| 현재 Method · Path | 현재 용도 | 최종 처리 |
| --- | --- | --- |
| `POST /api/v1/move-jobs/onboarding` | 소비자 작업과 소비자 capability 하나 생성 | 실제 소비자 onboarding 생성 경로로 사용 |
| `GET /api/v1/me` | 현재 역할·초대 상태·허용 기능 조회 | 역할별 landing에서 사용 |
| `POST /api/v1/move-jobs/{job_id}/invitations` | 다음 역할 참여자와 pending capability 생성 | 소비자→업체, 수락 업체→현장기사만 허용 |
| `GET /api/v1/move-jobs/{job_id}/invitations` | 본인이 발급했거나 받은 초대 상태 조회 | 초대 관리 화면에서 사용 |
| `POST /api/v1/move-jobs/{job_id}/invitations/{invitation_id}/accept` | 받은 초대 수락 | 수락 뒤에만 일반 업무 권한 활성화 |
| `POST /api/v1/move-jobs/{job_id}/invitations/{invitation_id}/decline` | 받은 초대 거절 | 현재 link 즉시 철회 |
| `POST /api/v1/move-jobs/{job_id}/invitations/{invitation_id}/revoke` | 발급한 초대 폐기 | link와 하위 초대 즉시 철회 |
| `POST /api/v1/move-jobs/{job_id}/invitations/{invitation_id}/reissue` | 발급한 초대 재발급 | 기존 link·하위 초대를 철회하고 pending link 한 번 반환 |
| `POST /api/v1/move-jobs` | 작업과 정확히 세 역할의 초기 capability 생성 | 신뢰 bootstrap·전달 채널 결정 전 일반 frontend 연동에서 제외 |
| `GET /api/v1/move-jobs/{job_id}` | 작업·참여자·공간 구성 조회 | 화면별 view에 필요한 header만 재사용 |
| `POST /api/v1/move-jobs/{job_id}/participants/{participant_id}/access-links` | 역할 link 재발급 | private bootstrap·운영으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/access-links/{access_link_id}/revoke` | 역할 link와 위임한 하위 초대 철회 | private bootstrap·운영으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions` | 촬영 session 생성 | media upload 내부 logic으로 재사용 |
| `GET /api/v1/move-jobs/{job_id}/capture-sessions` | 본인 세션·미디어 validation·분석 상태 복구 | 첫 촬영·AI E2E slice의 query로 바로 사용 가능; signed capability와 provider 내부값은 제외 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/submit` | READY inventory 촬영을 동결하고 분석 intent 생성 | 첫 촬영·AI E2E slice의 command로 바로 사용 가능; 화면용 adapter 여부는 FE 연동 후 결정 |
| `GET /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/analysis` | 분석 queue·실행·범위 초안 terminal 상태 조회 | 첫 촬영·AI E2E slice의 polling query로 바로 사용 가능; review item view는 별도 필요 |
| `GET /api/v1/move-jobs/{job_id}/analysis-review` | 최신 완료 분석의 공간별 검증 수·편집 항목 조회 | FE AI 검토 화면의 단일 query; model·prompt·provider task 정보는 제외 |
| `POST /api/v1/move-jobs/{job_id}/analysis-review/complete` | 고객 검토 결과를 불변 scope version으로 확정 | FE AI 검토 CTA의 단일 command; stale 원본과 상충 재전송은 `409` |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/upload` | signed upload URL 발급 | `POST /media-uploads`로 축소 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/{media_asset_id}/complete` | 업로드 metadata 검증·확정 | 이슈·완료 제출 시 server 검증으로 흡수 |
| `POST /api/v1/move-jobs/{job_id}/scope-versions` | 불변 범위 version 생성 | `POST /scope-proposals` 내부 logic으로 재사용 |
| `GET /api/v1/move-jobs/{job_id}/scope-versions` | 범위 version 이력 조회 | `GET /scope-review` 화면 view로 교체 |
| `POST /api/v1/move-jobs/{job_id}/scope-versions/{scope_version_id}/approvals` | 고객·업체 범위 확인 | 고객 `POST /scope-review/confirm`으로 전환 |
| `GET /api/v1/move-jobs/{job_id}/scope-review` | 현재 범위·견적·수정요청·양측 확인·AI 원본 preview 조회 | 고객·업체 범위 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/scope-proposals` | 업체가 현재 범위의 불변 자식과 원화 견적 snapshot 전송 | 업체 범위 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/scope-review/revision-request` | 고객이 현재 제안의 수정 요청 생성 | 고객 범위 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/scope-review/confirm` | 고객이 현재 업체 제안을 확인하고 양측 범위를 잠금 | 고객 범위 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/dispatch/setup` | 업체 연동이 현재 범위·일정의 요구사항과 차량·인력 후보 snapshot 등록 | 작업별 immutable resource 연동 경계; master CRUD는 별도 범위 |
| `GET /api/v1/move-jobs/{job_id}/dispatch` | 배차 요구사항·후보·충돌·현재 선택 조회 | 업체 배차 화면의 실행 계약 |
| `PUT /api/v1/move-jobs/{job_id}/dispatch` | 차량·인력 선택 원자 확정과 현장기사 알림 생성 | 업체 배차 CTA의 멱등 command |
| `GET /api/v1/move-jobs/{job_id}/field-brief` | 배정 기사에게 현재 범위·일정·현장 조건·checklist 제공 | 현장기사 상세 화면의 실행 계약; 신뢰 source 없는 연락 URI는 `null` |
| `POST /api/v1/move-jobs/{job_id}/check-ins` | 예정일 당일 checklist 전체 확인과 도착 기록 | 현장기사 체크인 CTA의 멱등 command |
| `POST /api/v1/move-jobs/{job_id}/field-issues` | 현장기사가 잠긴 범위에 무가격 이슈·READY 증거 보고 | 현장기사 이슈 화면의 실행 계약 |
| `GET /api/v1/move-jobs/{job_id}/field-issues` | 업체·현장기사의 이슈와 제안 처리 상태 조회 | 업체 후속 처리와 기사 상태 복구에 사용 |
| `POST /api/v1/move-jobs/{job_id}/change-proposals` | 업체가 이슈를 변경 범위·원화 견적 제안으로 전환 | 업체 현장 변경 command 실행 계약 |
| `GET /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}` | 고객·업체 변경 사유·증거 preview·견적·결정 기록 조회 | 고객 현장 변경 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/decision` | 고객 승인·거절·설명 요청 | 고객 CTA 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/explanation` | 업체가 고객 설명 요청에 답변 | 업체 설명 command 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/change-requests` | 현장 변경요청 생성 | `field-issues`와 업체 `scope-proposals`로 단계 분리 |
| `GET /api/v1/move-jobs/{job_id}/change-requests` | 현장 변경요청 목록 조회 | 변경 상세와 완료 요약 view에 흡수 |
| `GET /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/evidence/{media_asset_id}/read-url` | READY 변경 증거의 generation-pinned 열람 URL 발급 | 제안 화면 view의 signed preview와 통합 여부 검토 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/clarification` | 현장기사에게 설명 요청 | 최종 decision action으로 통합 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/explanation` | 현장기사 설명 제출 | 별도 화면 승인 전 frontend에서 제외 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/decision` | 현장 변경 승인·거절 | `change-proposals/{id}/decision`에 재사용 |
| `POST /api/v1/move-jobs/{job_id}/completion-submissions` | 대표 현장기사 완료 checklist·근무·현장 확인·선택적 미디어 제출 | 현장기사 완료 화면의 실행 계약; 정확 replay와 문제 뒤 정정 지원 |
| `GET /api/v1/move-jobs/{job_id}/completion-summary` | 완료·최종 금액·변경·요청·문서·보존 상태 조회 | 업체 완료와 고객 완료 확인 화면의 단일 view |
| `POST /api/v1/move-jobs/{job_id}/completion-requests` | 업체의 최신 완료 제출 고객 확인 요청 | 7일 만료와 고객 notification intent를 포함한 멱등 command |
| `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/revoke` | 업체의 살아 있는 완료 요청 철회 | 재점검·잘못 보낸 요청을 이력 보존 상태로 철회 |
| `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/decision` | 고객 완료 확인 또는 문제 신고 | 확인 시 완료·감사·보존을 원자 반영하고 문제는 책임판단 없이 별도 기록 |
| `GET /api/v1/move-jobs/{job_id}/documents/archive` | 견적·변경·완료·결정 PDF와 manifest ZIP | 업체 전용 결정적 문서 package; 준비 전 `409` |
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
없고 API CORS는 실제 공개 mutation에 필요한 `GET`, `POST`, `PUT`만 허용한다. 제안 계약은
OpenAPI에 등록된 뒤에만 frontend에 적용한다.
촬영 제출은 capture 소유자에게 멱등이며 제출 뒤 추가 media mutation을 막는다. 분석 상태 API는
`scope_version_id` 또는 provider-neutral 실패만 노출하므로, FE는 provider task ID나 오류 원문을
별도 계약으로 추정하지 않는다.

## 6. 운영 route 3개

| Method · Path | 용도 | frontend 사용 |
| --- | --- | --- |
| `GET /healthz` | process bootstrap 확인 | 사용하지 않음 |
| `GET /edgez` | 일반 public traffic 경로 확인 | 사용하지 않음 |
| `GET /readyz` | DB 연결 readiness 확인 | 사용하지 않음 |

## 7. 남은 backend·연동 범위

현재 승인된 v1 제품 계약 기준으로 A 소유의 무조건적인 backend 구현은 없다. 아래 항목은 FE 연동 결과나 B·외부 결정을 선행조건으로 둔 조건부 범위다.

### 조건부 계약·조회 재구성

- 완료 포함 화면 단위 view와 command는 구현됨
- 조건부 화면용 `media-uploads` adapter; 현재 capture 생성·upload·complete 상태 전이를 보존할 때만 추가
- 후속 공통 `JobHeader`, `ScopeLineV2`; `QuoteSnapshot`과 signed 범위·완료 preview v1은 구현됨
- 수량·단위·작업 메모가 필요한 AI `AnalysisResult` v2와 B consumer 영향 확인

### frontend 연동

- scope·변경·배차·완료 화면의 실제 API query·mutation 연동과 역할별 mock browser 회귀 6개는 완료됨
- 임시 HTTPS FE에서 고객 onboarding → 업체·기사 초대 → 견적·범위 확정 → 배차·체크인 → 완료 제출·요청·확정의 staging 실브라우저 E2E가 완료됨
- canonical HTTPS origin 구매·배포 뒤 API와 GCS CORS를 실제 origin으로 교체하고 signed PUT·READY polling을 브라우저에서 재검증

### migration

INT-01은 `int_01_0001`과 `capture_analysis_dispatch`, A-02는 `a_02_0002`와 `participant_invitation`, INT-02는 `int_02_0001`과 `scope_proposal`·`scope_revision_request`, INT-03은 `int_03_0002`와 `field_issue`·`field_issue_evidence`·`change_proposal_detail`, A-13은 `a_13_0001`과 `dispatch_setup`·`dispatch_plan`·`field_check_in`, INT-04는 `int_04_0001`과 `completion_submission`·`completion_submission_evidence`·`completion_request`·`completion_problem_report` 및 완료 checklist를 추가했다. 단일 head는 `int_04_0001`이며 기존 범위 version, 승인, 변경요청, media, audit, notification table을 재사용한다.

## 8. frontend 연동 기준

- 최신 main의 비운영 `/openapi.json`만 현재 실행 계약으로 사용한다.
- 현재 route 이름과 제안 경로를 섞어 임시 연동하지 않는다.
- 각 최종 endpoint가 OpenAPI에 추가되고 권한·오류·중복 호출 test가 통과한 뒤 연동한다.
- FE 공통 기반의 `VITE_API_BASE_URL`, API client와 TanStack Query 정책을 유지하고 capability secret은 화면에서도 메모리에만 보관한다.
- 화면 조회는 여러 CRUD 호출을 조합하지 않고 화면별 `GET` 한 번을 기본으로 한다.
- CTA 하나는 command endpoint 하나에 대응한다.
- 촬영 E2E는 FE #4, `analysis-review` 조회·완료는 FE #5, 충돌 복구는 FE #6으로 연결됐다. FE #8은 역할별 실행 계약 Playwright 검증을 CI에 추가했고 FE #9는 pending 초대의 보호 API 선호출을 차단했다.
- 2026-08-16의 임시 HTTPS canary가 끝난 뒤 API와 GCS CORS는 `https://34-160-87-130.sslip.io`로 원복했다. canonical FE origin이 생길 때 둘을 함께 교체한다.
