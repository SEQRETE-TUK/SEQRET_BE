# INT-09 프론트엔드 연동 인계

> 기준일: 2026-08-19
>
> Backend 작업 기준: `feat/int-09-frontend-contract`
>
> Frontend 대조 기준: `SEQRETE-TUK/SEQRET_FE` `origin/main`
> `437c0db22f5a7e5ca45acfa9e68b9325af4ce3cd`

이 문서는 백엔드가 제공하는 실행 계약과 프론트 담당자가 변경할 부분만 정리한다. 이 작업은
`SEQRET_FE` 파일을 수정하지 않는다.

## 최신 프론트 검증 결과

`437c0db` 기준 GitHub Actions는 `package.json`과 `pnpm-lock.yaml` 불일치로 dependency 설치
단계에서 실패한다. lockfile 검사를 우회해 실행한 TypeScript 검사에서도 다음 항목이 실패한다.

- 신규 업체·기사 workflow가 제거된 `lucide-react`를 import한다.
- `LiveProviderWorkflow` 호출부가 컴포넌트에 없는 `embedded` prop을 전달한다.
- 업체 `POST /scope-proposals` body에 필수 `execution_plan`이 없다. 실행계획은 견적 버전에 고정되는
  A-23 계약이므로 차량 수·설명, 작업자 수와 예상시간을 실제 입력값으로 보내야 한다.
- 기사 evidence·completion 업로드가 `createCaptureSession(connection)`만 호출한다. A-19 계약에 따라
  먼저 `GET /media-consent-policy`로 현재 정책을 표시하고 `consentPolicyVersion`과 명시적인
  `privacyNoticeAcknowledged: true`를 전달해야 한다.

현재 브라우저 계약 테스트는 `page.route("**/api/v1/**")`로 API 응답을 가로채므로 실제 staging
FastAPI, CORS, GCS signed upload, Cloud Tasks와 Vertex AI까지 검증하지 않는다. 위 빌드 오류와 아래
세션 연동을 반영한 뒤 staging origin을 대상으로 별도 E2E를 실행해야 한다.

## 해결한 네 가지 요청

| 요청 | 백엔드 실행 계약 |
| --- | --- |
| 업체·고객·기사 다중 작업 목록과 완료 기록 | `GET /api/v1/move-jobs`가 서버 작업공간에 연결된 모든 작업의 현재 요약을 반환한다. 상태·검색·예정일 필터와 최대 100개 제한을 지원한다. |
| 이사 기본정보 서버 수정 | `PATCH /api/v1/move-jobs/{job_id}`가 제목, 예정시각, 출·도착지 표시명과 구조화 현장 조건을 수정한다. 고객만 견적·완료·취소 전에 호출할 수 있다. |
| 실제 외부 알림 발송 | 명시적으로 동의한 이메일·SMS·카카오 연락처에 대해 NHN Cloud transactional API를 호출한다. 기존 `/notifications`의 in-app 이력은 그대로 유지한다. |
| 안전한 재접속과 업체 계정 | 검증된 역할 링크를 30일 서버 작업공간 계정·HttpOnly 세션에 연결한다. 브라우저 저장소에는 access secret이나 session secret을 보관하지 않는다. |

## 프론트 필수 변경

현재 `src/api/client.ts`의 일반 API 요청과 파일 다운로드는 `credentials: "omit"`다. 세션 cookie를
사용하는 요청은 `credentials: "include"`로 바꿔야 한다. GCS signed upload 요청은 API cookie와
무관하므로 현재처럼 `omit`을 유지한다.

1. 역할 링크 검증·초대 수락이 끝나면 같은 bearer로 `POST /api/v1/sessions`를 호출한다.
2. 앱 시작과 새로고침 때 bearer 없이 `GET /api/v1/session`을 호출한다.
3. 같은 역할의 다른 작업 링크를 입력하면 현재 cookie와 bearer를 함께
   `POST /api/v1/sessions`에 보내 멤버십을 추가한다. 서로 다른 역할을 같은 작업공간에 섞으면
   `409`다.
4. 목록 화면은 bearer 없이 `GET /api/v1/move-jobs`를 호출한다. bearer를 보내면 그 링크의 작업
   한 건만 반환하므로 다중 목록 복원에 사용하면 안 된다.
5. cookie로 `POST`, `PUT`, `PATCH`, `DELETE`를 호출할 때 `/session` 응답의 `csrf_token`을
   `X-SEQRET-CSRF` header로 보낸다. bearer 인증 command에는 이 header가 필요 없다.
6. 고객 기본정보 저장 버튼은 `PATCH /api/v1/move-jobs/{job_id}`를 호출하고 성공 응답을 query
   cache의 원본으로 사용한다. `moveDraftStorageKey`의 `sessionStorage` 값은 서버 원본으로 사용하지
   않는다.
7. 업체 web의 메모리 `connections` 배열과 mock 전용 다중 목록 대신 서버 목록을 사용한다.
8. 로그아웃은 `DELETE /api/v1/session`을 호출한 뒤 메모리의 `csrf_token`과 화면 상태를 지운다.

유효한 workspace cookie 없이 기존 작업의 bearer만 다시 제시하면 그 bearer가 증명한 작업 하나만
새 작업공간으로 재연결한다. 단일 초대키로 과거에 묶었던 다른 작업이나 계정 연락처까지 복구하지
않는다. 새 브라우저에서 여러 작업을 다시 묶으려면 각 작업의 secret을 차례로 다시 입력해야 한다.

access-link secret, workspace cookie 값과 CSRF token을 `localStorage`, `sessionStorage`, URL query,
analytics 또는 log에 저장하지 않는다. cookie는 `HttpOnly`여서 JavaScript에서 읽을 수 없고,
`csrf_token`은 `/session` 응답을 받은 현재 메모리에서만 사용한다.

## 세션과 목록 API

### `POST /api/v1/sessions`

```http
Authorization: Bearer <access-link-secret>
```

`201` 응답은 다음 형태이며 새 작업공간이면 `seqret_workspace_session` cookie도 설정한다.

```json
{
  "account_id": "uuid",
  "role": "company_manager",
  "display_name": "한결이사",
  "expires_at": "2026-09-18T06:00:00Z",
  "csrf_token": "opaque-memory-only-token",
  "members": [
    {
      "job_id": "uuid",
      "participant_id": "uuid",
      "role": "company_manager",
      "display_name": "한결이사",
      "invitation": null
    }
  ]
}
```

배포 환경 cookie 속성은 `HttpOnly; Secure; SameSite=None; Path=/api/v1; Max-Age=2592000`이다.
`GET /api/v1/session`은 같은 응답으로 새로고침 상태를 복원한다.

### `GET /api/v1/move-jobs`

| Query | 형식 | 의미 |
| --- | --- | --- |
| `status` | `draft|active|completed|canceled` | 작업 상태 |
| `q` | 1~100자 | 제목·고객명·업체명·현장기사명·위치 기본·상세 주소 부분 검색; 기사는 상세 주소 제외 |
| `scheduled_from` | timezone 포함 ISO 8601 | 예정시각 하한 |
| `scheduled_to` | timezone 포함 ISO 8601 | 예정시각 상한 |
| `limit` | 1~100, 기본 50 | 반환 상한 |
| `cursor` | 이전 응답의 opaque string | 다음 페이지 시작 위치 |

응답은 `{ "moves": MoveJobSummary[], "next_cursor": string|null }`이며 각 항목은 기존 FE `MockMoveSummary`와 같은
`job`, `version_label`, `scope_status`, `company_participation_status`,
`completion_request_status`, `quote`, `item_count`, `adjustment_count`를 가진다. 최근 생성한
작업부터 정렬한다. 같은 검색·필터와 `limit`을 유지한 채 `next_cursor`가 null이 될 때까지 조회하면
100건을 넘는 전체 기록을 순회할 수 있다. 검색·필터가 바뀌면 cursor를 폐기하고 첫 페이지부터
다시 조회한다.

## 기본정보 수정 API

`PATCH /api/v1/move-jobs/{job_id}`는 부분 수정이며 빈 body는 `422`다. `scheduled_at: null`은
일정을 미정으로 되돌린다. 기본 주소 `label`과 선택적 상세 주소 `detail_address`를 분리해 저장하고,
사다리차 사용 여부는 `conditions.ladder`에 `required|not_required|unknown`으로 저장한다. v2 범위
초안이 있으면 조건 변경을 새 불변 자식 버전에 snapshot한다. 이미 양측 확인으로
잠긴 범위의 조건 또는 견적·완료·취소 상태는 `409`이므로 최신 서버 값을 다시 조회해 안내한다.
상세 주소는 고객·업체 작업 응답에만 포함하고, 기사는 배차 확정 뒤 전용 `field-brief`에서만 받는다.

```json
{
  "title": "8월 22일 이사",
  "scheduled_at": "2026-08-22T13:30:00+09:00",
  "locations": [
    {
      "kind": "origin",
      "label": "서울 성동구 출발지",
      "detail_address": "101동 1203호",
      "conditions": {
        "residence_type": "apartment",
        "floor": {"status": "known", "value": 7},
        "elevator": "available",
        "stairs": "not_required",
        "ladder": "required",
        "parking_access": "restricted",
        "carry_distance": {"status": "known", "value_m": 20},
        "access_note": "지하주차장 진입 확인"
      }
    }
  ]
}
```

성공하면 갱신된 `MoveJob` 전체를 반환하고 `JOB_BASIC_INFO_UPDATED` 감사 이력을 남긴다. 견적이
한 번이라도 생성됐거나 작업이 완료·취소됐으면 `409`다.

## 외부 알림 연락처

연락처 API는 workspace cookie 인증만 사용한다.

| Method | Path | 설명 |
| --- | --- | --- |
| `GET` | `/api/v1/session/contact-points` | 마스킹된 활성 연락처 조회 |
| `PUT` | `/api/v1/session/contact-points/{email|sms|kakao}` | 명시적 동의와 연락처 저장·교체 |
| `DELETE` | `/api/v1/session/contact-points/{email|sms|kakao}` | 연락처 철회와 미발송 건 취소 |

```json
{
  "destination": "+821012345678",
  "delivery_consent": true,
  "enabled": true
}
```

이메일은 일반 이메일 형식, SMS·카카오는 한국 E.164 `+82...`만 허용한다. 응답에는 원문 대신
`masked_destination`만 포함한다. 연락처를 새로 등록해도 과거 event를 소급 발송하지 않으며,
이후 생성되는 알림부터 in-app intent와 별도 외부 delivery를 함께 만든다.

## 배포 도메인 주의

Vercel origin과 현재 `sslip.io` API는 서로 다른 site다. 백엔드는 credential CORS와
`SameSite=None; Secure`를 적용하지만 Safari 등 브라우저의 third-party cookie 정책에 따라 세션
지속이 차단될 수 있다. 운영에서 확실한 새로고침 복원을 보장하려면
`app.example.com`과 `api.example.com`처럼 같은 site의 custom domain을 사용한다. 프론트 배포
환경은 프론트 담당 범위이며 백엔드는 사용할 API origin과 credential CORS 조건만 전달한다.

## 외부 발송 활성화 경계

코드와 배포 설정은 준비됐지만 실제 발송은 NHN Cloud에 등록된 발신자·승인 템플릿과 Secret이
있어야 검증할 수 있다. 설정이 완전하지 않으면 relay가 시작 단계에서 실패하도록 구성하고,
기본값은 `SEQRET_NOTIFICATION_DELIVERY_ENABLED=false`다. 카카오 템플릿은 `#{message}`와
`#{deepLink}` 변수를 승인받아야 한다.
