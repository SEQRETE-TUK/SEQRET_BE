# SEQRET MVP API 구현 현황

> 기준일: 2026-08-19
>
> backend 기능 기준 코드: 이 문서를 포함한 최신 `main`
>
> frontend 확인 기준: `SEQRETE-TUK/SEQRET_FE` `origin/main`
> `00e13331b37d7405e7118d89f3dfc953db3e8402`
>
> 관련 문서: [API 명세](API_SPEC.md), [추가 화면 요청서](FRONTEND_SCREEN_REQUEST.md)
>
> 실행 계약의 단일 원본: 최신 `main` 코드와 비운영 환경의 `/openapi.json`

## 1. 한눈에 보기

| 구분 | 수량 | 의미 |
| --- | ---: | --- |
| 현재 FastAPI 등록 operation | 72개 | 59개 path의 업무 operation 69개 + 운영 operation 3개 |
| 최신 FE가 선언한 시각 demo 화면 | 27개 | 소비자 12 + 업체 mobile 6 + 업체 web 4 + 작업자 5; API E2E 증거 아님 |
| FE 실제 API 연동 범위 | capture + 3역할 | 촬영·분석과 고객·업체·현장기사의 초대, 범위·변경, 배차·체크인, 완료 흐름을 query·mutation으로 호출; 별도 signed PUT 포함 |
| 기존 8화면 기준 backend 목표 API | 17개 | 16개 구현; 화면용 `media-uploads` adapter만 조건부 잔여 |
| 승인된 P0 화면 포함 backend 목표 API | 19개 | 18개 구현; 완료 제출과 고객 결정 포함 |
| INT-12 추가 P0 조회 API | 2개 | 현장 이슈 증거 열람과 확인서 전체 이력 모두 구현 |
| 남은 목표 API | 1개 | 기존 capture 상태 전이를 보존할 upload adapter만 조건부 잔여 |

현재 route가 많다고 frontend 준비가 끝난 것은 아니다. frontend는 현재 69개 업무 operation과
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
| runtime | Vite 8.2.1, React 19.2.4, TypeScript | 최신 `00e13331` lockfile·source 기준 |
| 사용자 경로 | `/`, `/consumer/capture`, `/provider`, `/provider/web`, `/crew`, `/design-system` | 역할별 업무 경로와 capture가 실제 API client를 사용하고 design system만 독립 시각 경로 |
| 화면·상태 | PRD 기준 27개 화면과 `screen`, `view`, `state` query variant | PRD 수치는 시각 범위이며 현재 역할별 runtime workflow 수와 동일하지 않음 |
| server state | TanStack Query 기반 역할별 query·mutation·polling, command replay와 `409` refetch | 초대 pending 동안 보호 API를 호출하지 않으며 수락 뒤 역할 업무 query를 활성화함 |
| API 기반 | `VITE_API_BASE_URL`, `/api/v1` 제한, 명시적 Bearer, opaque signed PUT client 존재 | 일반 API도 `credentials: "omit"`이므로 workspace cookie 연동 전환이 필요하고 signed PUT만 omit 유지 |
| 다중 목록·기본정보 | FE 구현은 계속 변경 가능 | INT-12 cursor 목록·구조화 상세 주소와 사다리차·견적 후 수정 차단을 서버 계약으로 고정 |
| CI·배포 | FE Vercel 배포는 프론트 담당 범위 | 백엔드는 FE 코드·설정을 변경하지 않고 OpenAPI·CORS 계약만 제공 |

최신 대조 대상 `00e13331` 이후에도 FE는 계속 변경될 수 있다. backend 완료 여부는 FE의 mock·local
상태와 분리하고 OpenAPI, migration과 서버 회귀로 판단한다. 정확한 연동 계약은
[INT-12 FE 인계](INT_12_FRONTEND_HANDOFF.md)에 있다.

### 3.2 계약 불일치

| 항목 | 최신 FE PRD 부록 | 현재 backend 실행 계약 |
| --- | --- | --- |
| base path | `https://api.{service}.kr/v1` | `/api/v1` |
| 역할 | `consumer`, `provider`, `crew` | `customer`, `company_manager`, `field_worker` |
| 오류 body | `{error: {code, message, request_id}}` | FastAPI `detail`; 일부 응답에 `x-request-id` |
| 업무 경로 | `/jobs`, `/change-orders`, `/assignment`, `/completion/*` 등 | `/sessions`, `/session/contact-points`, `/move-jobs`, `/invitations`, `/media-consent-policy`, `/capture-sessions/*/submit`, `/analysis-review`, `/scope-review`, `/dispatch`, `/field-brief`, `/check-ins`, `/completion-*` 등 69개 operation |
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
| 업체 범위 | `POST /api/v1/move-jobs/{job_id}/scope-proposals` | 범위·금액·실행계획을 고객에게 전송 | 구현 | 범위 v1·v2, 원화 견적, 차량·인원·예상시간, 포함·제외·사유를 저장하고 업체 확인을 함께 기록 |
| 고객 AI 검토 | `GET /api/v1/move-jobs/{job_id}/analysis-review` | 업로드와 AI 검토 초안 조회 | 구현 | v1/v2 품목, 위치조건 snapshot·AI 제안과 공간별 media 수를 provider-neutral view로 반환 |
| 고객 AI 검토 | `POST /api/v1/move-jobs/{job_id}/analysis-review/complete` | 고객 수정 결과를 업체 검토 초안으로 제출 | 구현 | 구조화 품목·전체 위치조건 고객 편집본을 불변 자식으로 생성하고 동일 payload 재전송은 멱등 처리 |
| 고객 변경 승인 | `GET /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}` | 현장 변경 사유, 증빙, 금액 조회 | 구현 | 변경요청·견적 snapshot과 generation-pinned UPLOADED·READY preview를 한 view로 반환; 승인은 READY에서만 처리 |
| 고객 변경 승인 | `POST /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/decision` | 승인·거절·설명 요청 | 구현 | 고객 전용 결정, 정확 replay, 승인 시 양측 확인·scope lock을 원자적으로 수행 |
| 업체 배차 | `GET /api/v1/move-jobs/{job_id}/dispatch` | 차량·작업자 후보와 충돌 조회 | 구현 | 현재 범위에 묶인 작업별 immutable 후보 snapshot, 요구사항·충돌·선택 상태 반환 |
| 업체 배차 | `PUT /api/v1/move-jobs/{job_id}/dispatch` | 배정 확정과 알림 생성 | 구현 | 용량·인원·기술·자격·대표 기사를 원자 검증하고 `dispatch_confirmed.v1` 알림 연결 |
| 업체·고객 완료 | `GET /api/v1/move-jobs/{job_id}/completion-summary` | 완료 사진, 근무, 변경, 금액, 요청·문서 요약 | 구현 | 업체와 요청받은 고객의 단일 view; 체크리스트 항목과 UPLOADED·READY generation-pinned preview 포함, 최종 확인은 READY에서만 처리 |
| 업체 완료 | `POST /api/v1/move-jobs/{job_id}/completion-requests` | 고객에게 7일 완료 확인 요청 | 구현 | 최신 제출·활성 요청·정확 replay 검증과 고객 알림 intent 연결 |
| 업체 완료 | `GET /api/v1/move-jobs/{job_id}/documents/archive` | 증빙 PDF 4종·manifest ZIP 다운로드 | 구현 | 결정적 archive; 준비 전 `409`, 완료 DB 사실과 생성 실패 분리 |
| 현장기사 범위 | `GET /api/v1/move-jobs/{job_id}/field-brief` | 최신 범위, 경로, 일정, 담당자와 현장 조건 조회 | 구현 | 확정 배정·현재 잠긴 범위·마스킹 위치·checklist와 체크인 상태를 한 view로 반환 |
| 현장기사 범위 | `POST /api/v1/move-jobs/{job_id}/check-ins` | 현장 도착 시각 기록 | 구현 | 배정된 대표 기사, 예정일 당일과 checklist 전체 확인을 검증하고 정확 replay 허용 |
| 현장기사 이슈 | `POST /api/v1/move-jobs/{job_id}/media-uploads` | 이슈 증빙 signed upload URL 발급 | 전환 | 기존 capture 3단계와 `StoragePort` 재사용; frontend path 단순화 |
| 현장 이슈 | `POST /api/v1/move-jobs/{job_id}/field-issues` | 범위 밖 작업·파손 위험·현장 장애 보고 | 구현 | 잠긴 범위와 보고자 소유 UPLOADED·READY 증거를 검증하고 무가격 이슈와 업체 견적 단계를 분리; 고객도 목록 조회 가능하고 승인은 READY 필수 |

### 4.2 승인된 추가 P0 화면 2개

| 화면 | Method · Path | 용도 | 상태 | 재사용 기반·남은 일 |
| --- | --- | --- | --- | --- |
| 현장기사 완료 기록 | `POST /api/v1/move-jobs/{job_id}/completion-submissions` | 완료 사진, 체크리스트, 실제 근무와 현장 확인 제출 | 구현 | 체크인·현재 배차·범위·작업자·선택적 upload-complete 미디어 검증과 정정 제출 지원; 고객 최종 확인은 READY 필수 |
| 고객 완료 확인 | `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/decision` | 완료 확인 또는 문제 신고 | 구현 | 책임 자동판단 없이 문제를 분리하고 확인 시 완료·보존 intent를 원자 반영 |

두 P0 API는 승인되어 [API 명세](API_SPEC.md)와 OpenAPI에 편입됐다. 목표 19개 중 화면용 upload adapter를 제외한 18개가 구현됐다.

### 4.3 INT-12 추가 P0 조회 2개

| 화면 | Method · Path | 용도 | 상태 | 안전 경계 |
| --- | --- | --- | --- | --- |
| 고객·업체·기사 현장 이슈 | `GET /api/v1/move-jobs/{job_id}/field-issues/{field_issue_id}/evidence/{media_asset_id}/read-url` | 변경안 작성 전 증거 사진 열람 | 구현 | 같은 작업 참여자·이슈·media·purpose와 READY generation을 검사하고 5분 HTTPS URL만 반환 |
| 고객·업체 확인서 이력 | `GET /api/v1/move-jobs/{job_id}/scope-review/history` | 버전별 범위·견적·포함/제외·양측 확인 역할과 시각 조회 | 구현 | 고객·업체만 허용하고 불변 version 오름차순으로 전체 이력을 반환; 기사는 `403` |

두 조회는 기존 `FieldIssueResponse`, `/scope-versions`, 현재 `/scope-review` 응답을 교체하지 않는
추가형 계약이다. 기존 소비자와 호환 API는 그대로 유지한다.

### 4.4 온보딩·조건부 API

| 조건 | 추가 API | 결정 기준 |
| --- | --- | --- |
| 고객·업체가 최초 사진을 직접 등록 | 화면용 `capture-submissions` adapter | 현재 저수준 capture 생성·upload·complete·submit·status 계약을 한 화면 command/view로 감쌀 때만 추가 |
| 소비자가 작업을 직접 생성 | `POST /api/v1/move-jobs/onboarding` | 구현. 고객 참여자와 고객 secret 하나만 발급 |
| 소비자가 업체를, 업체가 현장기사를 초대 | `/me`, invitation 생성·목록·수락·거절·폐기·재발급 | 구현. pending 업무 접근 차단과 상위 철회 cascade 포함 |
| 역할별 여러 작업과 새로고침 복원 | `/sessions`, `/session`, `GET /move-jobs` | 구현. 30일 HttpOnly cookie, CSRF, 동일 역할 멤버십 목록·검색·필터 포함 |
| 고객 기본정보 서버 수정 | `PATCH /api/v1/move-jobs/{job_id}` | 구현. 기본·상세 주소와 사다리차를 포함하고 견적이 한 번이라도 생성되면 전체 수정 `409`; v2 조건은 새 불변 snapshot에 반영 |
| 실제 이메일·SMS·카카오 전달 | contact-point API와 relay NHN adapter | 서버 기능 플래그로 유지. 현재 FE에는 설정 화면이 없어 추가 연동 대상에서 제외 |

현장 변경 화면을 지원하기 위해 목표 17개 밖의 업체용 `GET /field-issues`,
`POST /change-proposals`, `POST /change-proposals/{id}/explanation`도 실행 계약으로 등록됐다.

## 5. 현재 서버 업무 operation 69개

이 표는 현재 호출 가능한 API의 용도와 최종 처리 방향이다. `유지`는 frontend 공개 유지를 뜻하지 않고 내부 bootstrap·운영 용도로 보존한다는 의미다.

| 현재 Method · Path | 현재 용도 | 최종 처리 |
| --- | --- | --- |
| `POST /api/v1/move-jobs/onboarding` | 소비자 작업과 소비자 capability 하나 생성 | 실제 소비자 onboarding 생성 경로로 사용 |
| `GET /api/v1/me` | 현재 역할·초대 상태·허용 기능 조회 | 역할별 landing에서 사용 |
| `POST /api/v1/sessions` | active 역할 link를 서버 작업공간 계정에 연결 | 최초 연결과 동일 역할의 추가 작업 연결에 사용 |
| `GET /api/v1/session` | HttpOnly cookie로 계정·멤버십·CSRF 복원 | 앱 시작·새로고침 query로 사용 |
| `DELETE /api/v1/session` | 현재 workspace session 철회 | cookie + CSRF 로그아웃 command |
| `GET /api/v1/session/contact-points` | 마스킹된 외부 알림 연락처 목록 | 운영 기능으로 유지; 현재 FE 연동 대상에서 제외 |
| `PUT /api/v1/session/contact-points/{channel}` | 명시적 동의 연락처 저장·교체 | 운영 기능으로 유지; 현재 FE에 설정 화면 추가하지 않음 |
| `DELETE /api/v1/session/contact-points/{channel}` | 연락처 철회와 미발송 row 중단 | 운영 기능으로 유지; 현재 FE 연동 대상에서 제외 |
| `POST /api/v1/move-jobs/{job_id}/invitations` | 다음 역할 참여자와 pending capability 생성 | 소비자→업체, 수락 업체→현장기사만 허용 |
| `GET /api/v1/move-jobs/{job_id}/invitations` | 본인이 발급했거나 받은 초대 상태 조회 | 초대 관리 화면에서 사용 |
| `POST /api/v1/move-jobs/{job_id}/invitations/{invitation_id}/accept` | 받은 초대 수락 | 수락 뒤에만 일반 업무 권한 활성화 |
| `POST /api/v1/move-jobs/{job_id}/invitations/{invitation_id}/decline` | 받은 초대 거절 | 현재 link 즉시 철회 |
| `POST /api/v1/move-jobs/{job_id}/invitations/{invitation_id}/revoke` | 발급한 초대 폐기 | link와 하위 초대 즉시 철회 |
| `POST /api/v1/move-jobs/{job_id}/invitations/{invitation_id}/reissue` | 발급한 초대 재발급 | 기존 link·하위 초대를 철회하고 pending link 한 번 반환 |
| `POST /api/v1/move-jobs` | 작업과 정확히 세 역할의 초기 capability 생성 | 신뢰 bootstrap·전달 채널 결정 전 일반 frontend 연동에서 제외 |
| `GET /api/v1/move-jobs` | workspace 다중 작업 목록·검색·상태·예정일 필터·cursor 순회 | 고객·업체·기사 목록과 완료 기록의 서버 원본; 날짜 필터는 유지하되 현재 FE UI 추가 불필요 |
| `GET /api/v1/move-jobs/{job_id}` | 작업·참여자·공간 구성 조회 | 고객·업체 상세주소 제공, 기사는 숨김; 배차 확정 뒤 `field-brief`에서만 전용 공개 |
| `PATCH /api/v1/move-jobs/{job_id}` | 고객의 견적 전 기본정보 부분 수정 | 상세주소·사다리차 왕복 복원과 감사 기록; 견적 생성 뒤 전체 `409` |
| `DELETE /api/v1/move-jobs/{job_id}` | 고객의 견적 전 작업 취소와 모든 작업 capability 철회 | FE 작업 삭제 버튼의 실행 계약; 이력은 물리 삭제하지 않고 `CANCELED`로 보존 |
| `POST /api/v1/move-jobs/{job_id}/participants/{participant_id}/access-links` | 역할 link 재발급 | private bootstrap·운영으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/access-links/{access_link_id}/revoke` | 역할 link와 위임한 하위 초대 철회 | private bootstrap·운영으로 유지 |
| `GET /api/v1/move-jobs/{job_id}/media-consent-policy` | 현재 동의문 버전·처리 목적·보관기간 조회 | 촬영 전 동의 화면의 server source |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions` | 명시적 미디어 동의 snapshot과 촬영 session 생성 | 현재 정책 버전과 안내 확인 없이는 생성 거부 |
| `GET /api/v1/move-jobs/{job_id}/capture-sessions` | 본인 세션·미디어 validation·분석 상태 복구 | 첫 촬영·AI E2E slice의 query로 바로 사용 가능; signed capability와 provider 내부값은 제외 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/submit` | READY inventory 촬영을 동결하고 분석 intent 생성 | 첫 촬영·AI E2E slice의 command로 바로 사용 가능; 화면용 adapter 여부는 FE 연동 후 결정 |
| `GET /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/analysis` | 분석 queue·실행·범위 초안 terminal 상태 조회 | 실패 시 `failure_stage`·`provider_status`·`failure_detail_code`를 안전한 진단값으로 반환; 원문·provider task는 제외 |
| `GET /api/v1/move-jobs/{job_id}/analysis-review` | 최신 완료 분석의 공간별 검증 수·편집 항목 조회 | FE AI 검토 화면의 단일 query; model·prompt·provider task 정보는 제외 |
| `POST /api/v1/move-jobs/{job_id}/analysis-review/complete` | 고객 검토 결과를 불변 scope version으로 확정 | FE AI 검토 CTA의 단일 command; stale 원본과 상충 재전송은 `409` |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/upload` | signed upload URL 발급 | `POST /media-uploads`로 축소 |
| `POST /api/v1/move-jobs/{job_id}/capture-sessions/{capture_session_id}/media-assets/{media_asset_id}/complete` | 업로드 metadata 검증·확정 | 이슈·완료 제출 시 server 검증으로 흡수 |
| `POST /api/v1/move-jobs/{job_id}/scope-versions` | 불변 범위 version 생성 | `POST /scope-proposals` 내부 logic으로 재사용 |
| `GET /api/v1/move-jobs/{job_id}/scope-versions` | 범위 version 이력 조회 | `GET /scope-review` 화면 view로 교체 |
| `POST /api/v1/move-jobs/{job_id}/scope-versions/{scope_version_id}/approvals` | 고객·업체 범위 확인 | 고객 `POST /scope-review/confirm`으로 전환 |
| `GET /api/v1/move-jobs/{job_id}/scope-review` | 현재 범위·견적·수정요청·양측 확인·AI 원본 preview 조회 | 고객·업체 범위 화면의 실행 계약 |
| `GET /api/v1/move-jobs/{job_id}/scope-review/history` | 모든 불변 범위의 견적·포함/제외·확인 역할과 시각 조회 | 고객·업체 확인서 이력 화면의 실행 계약; 현장기사 제외 |
| `POST /api/v1/move-jobs/{job_id}/scope-proposals` | 업체가 현재 범위의 불변 자식과 원화 견적 snapshot 전송 | 업체 범위 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/scope-review/revision-request` | 고객이 현재 제안의 수정 요청 생성 | 고객 범위 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/scope-review/confirm` | 고객이 현재 업체 제안을 확인하고 양측 범위를 잠금 | 고객 범위 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/dispatch/setup` | 업체 연동이 현재 범위·일정의 요구사항과 차량·인력 후보 snapshot 등록 | 작업별 immutable resource 연동 경계; master CRUD는 별도 범위 |
| `GET /api/v1/move-jobs/{job_id}/dispatch` | 배차 요구사항·후보·충돌·현재 선택 조회 | 업체 배차 화면의 실행 계약 |
| `PUT /api/v1/move-jobs/{job_id}/dispatch` | 차량·인력 선택 원자 확정과 현장기사 알림 생성 | 업체 배차 CTA의 멱등 command |
| `GET /api/v1/move-jobs/{job_id}/field-brief` | 배정 기사에게 현재 범위·일정·현장 조건·checklist 제공 | 현장기사 상세 화면의 실행 계약; 신뢰 source 없는 연락 URI는 `null` |
| `POST /api/v1/move-jobs/{job_id}/check-ins` | 예정일 당일 checklist 전체 확인과 도착 기록 | 현장기사 체크인 CTA의 멱등 command |
| `POST /api/v1/move-jobs/{job_id}/field-issues` | 업체·현장기사가 잠긴 범위에 무가격 이슈·READY 증거 보고 | 현장 이슈 화면의 실행 계약 |
| `GET /api/v1/move-jobs/{job_id}/field-issues` | 고객·업체·현장기사의 이슈와 제안 처리 상태 조회 | 역할별 현장 보고 상태와 증거 preview에 사용 |
| `GET /api/v1/move-jobs/{job_id}/field-issues/{field_issue_id}/evidence/{media_asset_id}/read-url` | 고객·업체·기사의 READY 이슈 증거 열람 URL 발급 | 변경안 작성 전 5분 generation-pinned preview |
| `POST /api/v1/move-jobs/{job_id}/change-proposals` | 업체가 이슈를 변경 범위·원화 견적 제안으로 전환 | 업체 현장 변경 command 실행 계약 |
| `GET /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}` | 고객·업체 변경 사유·증거 preview·견적·결정 기록 조회 | 고객 현장 변경 화면의 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/decision` | 고객 승인·거절·설명 요청 | 고객 CTA 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}/explanation` | 업체가 고객 설명 요청에 답변 | 업체 설명 command 실행 계약 |
| `POST /api/v1/move-jobs/{job_id}/change-requests` | 현장 변경요청 생성 | 호환 소비자 확인 전 삭제 금지; 현재 FE는 `field-issues`·`change-proposals` 사용 |
| `GET /api/v1/move-jobs/{job_id}/change-requests` | 현장 변경요청 목록 조회 | 호환 소비자 확인 전 삭제 금지; 현재 FE 연동 대상에서 제외 |
| `GET /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/evidence/{media_asset_id}/read-url` | READY 변경 증거의 generation-pinned 열람 URL 발급 | 제안 화면 view의 signed preview와 통합 여부 검토 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/clarification` | 현장기사에게 설명 요청 | 최종 decision action으로 통합 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/explanation` | 현장기사 설명 제출 | 별도 화면 승인 전 frontend에서 제외 |
| `POST /api/v1/move-jobs/{job_id}/change-requests/{change_request_id}/decision` | 현장 변경 승인·거절 | `change-proposals/{id}/decision`에 재사용 |
| `POST /api/v1/move-jobs/{job_id}/completion-submissions` | 대표 현장기사 완료 checklist·근무·현장 확인·선택적 미디어 제출 | 현장기사 완료 화면의 실행 계약; 정확 replay와 문제 뒤 정정 지원 |
| `GET /api/v1/move-jobs/{job_id}/completion-summary` | 완료·최종 금액·변경·요청·문서·보존 상태 조회 | 업체 완료와 고객 완료 확인 화면의 단일 view |
| `POST /api/v1/move-jobs/{job_id}/completion-requests` | 업체의 최신 완료 제출 고객 확인 요청 | 7일 만료와 고객 notification intent를 포함한 멱등 command |
| `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/revoke` | 업체의 살아 있는 완료 요청 철회 | 서버 호환 기능으로 유지; 현재 FE에 철회 버튼 추가하지 않음 |
| `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/decision` | 고객 완료 확인 또는 문제 신고 | 확인 시 완료·감사·보존을 원자 반영하고 문제는 책임판단 없이 별도 기록 |
| `GET /api/v1/move-jobs/{job_id}/documents/archive` | 견적·변경·완료·결정 PDF와 manifest ZIP | 유지하되 현재 FE에 다운로드 버튼 추가하지 않음 |
| `POST /api/v1/move-jobs/{job_id}/completion-confirmations` | 고객·업체 완료 확인 | 호환 소비자 확인 전 삭제 금지; 현재 FE는 완료 요청·결정 흐름 사용 |
| `GET /api/v1/move-jobs/{job_id}/completion-confirmations` | 완료 확인 이력 조회 | 호환 소비자 확인 전 삭제 금지; 현재 FE는 `completion-summary` 사용 |
| `GET /api/v1/move-jobs/{job_id}/audit-events` | 전체 감사 이력 조회 | 내부 유지; 화면에는 필요한 요약만 제공 |
| `GET /api/v1/move-jobs/{job_id}/notifications` | 참여자 in-app·외부 전달 상태 이력 조회 | 현재 알림함 이력에 사용; 읽음·안 읽음 처리는 제외 |
| `POST /api/v1/move-jobs/{job_id}/background-jobs` | 보존 삭제 등 background job 생성 | 내부 운영 기능으로 유지 |
| `GET /api/v1/move-jobs/{job_id}/background-jobs` | background job 상태·오류 조회 | 내부 운영 기능으로 유지 |
| `POST /api/v1/move-jobs/{job_id}/background-jobs/{background_job_id}/retry` | 실패·lease 만료 job 재실행 | 내부 운영 기능으로 유지 |

현재 upload 응답은 provider 값을 해석하지 않는 `upload_url`·`upload_headers`를 반환하고,
별도 complete command가 generation·MIME type·크기를 고정한 비동기 validation intent를 만든다.
제안 `POST /media-uploads`가 complete API를 흡수하려면 이 상태 전이와 재시도 계약을 먼저
versioned contract로 승인해야 한다. 현재 HTTP mutation 전체에 `Idempotency-Key` 공통 header는
없고 API CORS는 credential 요청과 `DELETE`, `GET`, `PATCH`, `POST`, `PUT`을 허용한다. 제안 계약은
OpenAPI에 등록된 뒤에만 frontend에 적용한다.
촬영 제출은 capture 소유자에게 멱등이며 제출 뒤 추가 media mutation을 막는다. 분석 상태 API는
`scope_version_id` 또는 provider-neutral 실패를 노출한다. 실패에는 `failure_code`·`retryable`과
`failure_stage`·선택적 HTTP `provider_status`·`failure_detail_code`가 포함되며, FE는 provider task ID,
GCS URI, 모델 응답과 오류 원문을 별도 계약으로 추정하거나 표시하지 않는다.

## 6. 운영 route 3개

| Method · Path | 용도 | frontend 사용 |
| --- | --- | --- |
| `GET /healthz` | process bootstrap 확인 | 사용하지 않음 |
| `GET /edgez` | 일반 public traffic 경로 확인 | 사용하지 않음 |
| `GET /readyz` | DB 연결 readiness 확인 | 사용하지 않음 |

## 7. 남은 backend·연동 범위

INT-12까지 현재 backend는 전체 순회 가능한 다중 작업, 구조화 상세 주소·사다리차를 포함한
기본정보 수정, 권한 검사된 현장 이슈 증거 열람, 버전별 확인서 이력과 서버 세션을 제공한다. 견적
생성 뒤 기본정보 수정은 차단한다. 현재 FE에 없는 외부 전달 설정·ZIP·운영 API는 유지하되 추가
연동 범위로 잡지 않는다. 남은 항목은 FE 전환 또는 실제 배포 증거이며 새 backend CRUD 공백이 아니다.

### 조건부 계약·조회 재구성

- 완료 포함 화면 단위 view와 command는 구현됨
- A-24에서 현장 변경 승인 뒤 기사 `field-brief`가 변경 누적 견적과 기존 포함·제외 작업을 복원하도록 보완됨
- 조건부 화면용 `media-uploads` adapter; 현재 capture 생성·upload·complete 상태 전이를 보존할 때만 추가
- 후속 공통 `JobHeader`; `ScopeItemV2`, `LocationConditions`, `QuoteSnapshot`, 공동확인 상태와 signed 범위·완료 preview는 구현됨
- A-21의 AI `AnalysisResult` v2 범위 가져오기·고객 검수 API와 B-08의 영속화·Vertex v2 출력은 구현됨. FE 검수 UI 연결만 남음
- 2026-08-17 최신 main staging에서 실제 Vertex v2 분석 → A 범위 import → 고객 검수 완료·replay, 업체 시작 현장 이슈 → 고객 승인 → A-24 `field-brief` 누적 견적 복원까지 검증됨

### frontend 연동

- 최신 FE `00e13331`의 일반 API·download 요청을 `credentials: "include"`로 전환하고 signed GCS PUT만 `omit`으로 유지
- 역할 링크 검증 뒤 `/sessions`, 앱 시작 때 `/session`, 다중 목록에 `GET /move-jobs`, 고객 저장 버튼에 `PATCH /move-jobs/{id}` 연결
- cookie mutation에 메모리 전용 `X-SEQRET-CSRF`를 붙이고 메모리 provider connection과 기본정보 `sessionStorage`를 서버 원본으로 사용하지 않도록 전환
- Vercel과 `sslip.io`의 cross-site cookie 제한을 피하려면 운영 FE/API를 같은 site의 custom domain으로 배치
- FE 구현 진행 상태와 별개로 backend의 scope·변경·배차·완료·목록 계약과 secret 비저장 원칙을 유지함
- 최신 FE와 로컬 backend 실브라우저에서 고객 onboarding `201`, 기본 업체명 초대 `201`, 이사 취소 `204`, 취소 뒤 기존 token `401`을 확인함
- 확인서 탭을 나갔다 다시 들어오면 FE가 기존 pending 초대를 조회하지 않고 `POST /invitations`를 재호출해 `409`와 비활성 공유 버튼을 표시함. 기존 secret을 backend가 복구할 수 없으므로 FE가 invitation 상태를 조회해 발급·재발급을 구분해야 함
- 임시 HTTPS FE에서 고객 onboarding → 업체·기사 초대 → 견적·범위 확정 → 배차·체크인 → 완료 제출·요청·확정의 staging 실브라우저 E2E가 완료됨
- 프론트 담당자가 최신 backend 계약을 반영한 뒤 session cookie, signed PUT·READY polling과 역할별 목록을 브라우저에서 재검증
- NHN Cloud 발신자·템플릿 승인과 세 Secret을 준비한 뒤 외부 delivery를 별도 rollout에서 활성화하고 실제 테스트 수신처로 채널별 canary 수행

### migration

INT-01은 `int_01_0001`과 `capture_analysis_dispatch`, A-02는 `a_02_0002`와 `participant_invitation`, INT-02는 `int_02_0001`과 `scope_proposal`·`scope_revision_request`, INT-03은 `int_03_0002`와 `field_issue`·`field_issue_evidence`·`change_proposal_detail`, A-13은 `a_13_0001`과 `dispatch_setup`·`dispatch_plan`·`field_check_in`, INT-04는 `int_04_0001`과 `completion_submission`·`completion_submission_evidence`·`completion_request`·`completion_problem_report` 및 완료 checklist를 추가했다. A-16은 `a_16_0001`과 `location.conditions`, A-19는 `a_19_0001`과 촬영별 미디어 동의 snapshot을 추가했다. B-08은 `b_08_0001`과 AI v2 품목 필드·`analysis_location_condition_suggestion`을 추가했고 A-23은 `a_23_0001`과 `scope_proposal.execution_plan`을 추가했다. INT-09는 `int_09_0001`과 workspace account·membership·session·contact point, 기본정보 수정 감사와 외부 notification delivery 필드를 추가했다. INT-12는 `int_12_0001`과 `location.detail_address`, location·scope·change snapshot의 사다리차 backfill 및 scope hash 재계산을 추가했다. INT-17은 `int_17_0001`과 B 실행·A 공개 상태의 실패 단계, provider 상태, 안전한 세부 코드를 추가했다. 단일 head는 `int_17_0001`이며 A-20·A-21·A-22는 DB migration 없이 versioned 계약과 기존 JSON scope·analysis source를 재사용한다.

## 8. frontend 연동 기준

- 최신 main의 비운영 `/openapi.json`만 현재 실행 계약으로 사용한다.
- 현재 route 이름과 제안 경로를 섞어 임시 연동하지 않는다.
- 각 최종 endpoint가 OpenAPI에 추가되고 권한·오류·중복 호출 test가 통과한 뒤 연동한다.
- FE 공통 기반의 `VITE_API_BASE_URL`, API client와 TanStack Query 정책을 유지한다. capability secret과 CSRF token은 메모리에만 두고 workspace secret은 HttpOnly cookie로만 처리한다.
- 화면 조회는 여러 CRUD 호출을 조합하지 않고 화면별 `GET` 한 번을 기본으로 한다.
- CTA 하나는 command endpoint 하나에 대응한다.
- 촬영 E2E는 FE #4, `analysis-review` 조회·완료는 FE #5, 충돌 복구는 FE #6으로 연결됐다. FE #8은 역할별 실행 계약 Playwright 검증을 CI에 추가했고 FE #9는 pending 초대의 보호 API 선호출을 차단했다.
- 2026-08-16의 임시 HTTPS canary가 끝난 뒤 API와 GCS CORS는 `https://34-160-87-130.sslip.io`로 원복했다. 현재 canonical FE origin은 `https://seqret.vercel.app`이며 다음 backend 배포에서 둘을 함께 교체한다.
