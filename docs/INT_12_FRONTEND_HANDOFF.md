# INT-12 프론트엔드 계약 연동 인계

> Backend base: `74e10591653bd4027f4782f11255983e0e5b25f7`
>
> Frontend 확인 기준: `SEQRETE-TUK/SEQRET_FE` `origin/main`
> `00e13331b37d7405e7118d89f3dfc953db3e8402`

이 문서는 프론트엔드 수정자가 목록 페이지네이션, 이사 기본정보, 현장 이슈 증거와 확인서 이력을
연결할 때 필요한 backend 계약만 정리한다. 프론트엔드 구현·mock 여부나 배포 설정을 backend 완료
조건으로 삼지 않으며, backend는 프론트엔드 저장소나 Vercel 설정을 변경하지 않는다.

## 1. 작업 목록 전체 순회

```http
GET /api/v1/move-jobs?limit=50&cursor=<opaque-cursor>
```

```json
{
  "moves": [],
  "next_cursor": "opaque-cursor-or-null"
}
```

- 첫 요청은 `cursor`를 생략한다.
- 다음 요청은 직전 응답의 `next_cursor`를 해석하거나 변경하지 않고 그대로 보낸다.
- `next_cursor`가 `null`이면 마지막 페이지다.
- 다음 페이지에서도 `status`, `q`, `scheduled_from`, `scheduled_to`, `limit`을 동일하게 유지한다.
- 검색어, 필터 또는 `limit`을 바꾸면 기존 cursor를 버리고 첫 페이지부터 다시 조회한다.
- `limit`은 1~100이고 기본값은 50이다.

cursor는 서버 내부 구현을 감춘 불투명 값이다. client가 offset이나 식별자로 파싱하면 안 된다.

## 2. 상세 주소와 사다리차

생성·조회·수정의 각 location은 다음 필드를 사용한다.

```json
{
  "kind": "origin",
  "label": "서울시 중구 세종대로 110",
  "detail_address": "101동 1203호",
  "conditions": {
    "ladder": "required"
  }
}
```

- `label`: 기본 주소
- `detail_address`: 상세 주소, `string | null`, 입력 시 1~200자
- `conditions.ladder`: `required | not_required | unknown`
- 화면의 `사용`은 `required`, `사용 안 함`은 `not_required`로 보낸다.
- 기존 데이터의 상세 주소는 `null`, 사다리차 상태는 `unknown`으로 조회될 수 있다.
- 빈 상세 주소 입력은 `detail_address: null`로 보내면 기존 값을 지운다.
- 고객·업체의 `GET /move-jobs`, `GET /move-jobs/{job_id}`에는 상세 주소가 포함된다.
- 현장기사의 일반 작업 조회·목록에서는 상세 주소가 항상 `null`이고 `q`도 상세 주소를 검색하지
  않는다.
- 배차가 확정된 담당 현장기사는 `GET /field-brief`의 `origin_detail_address`,
  `destination_detail_address`에서만 상세 주소를 받는다.

`PATCH`의 `conditions`는 전달한 location의 조건 전체를 교체하는 계약이다. 한 조건만 바꿀 때도
현재 location의 `conditions`를 먼저 복사하고 변경할 필드만 덮어쓴 전체 객체를 보내야 한다.
온보딩과 기본정보 저장은 이 값을 `sessionStorage`에만 보관하지 말고 서버 요청에 포함한다.

## 3. 견적 생성 후 수정 차단

업체의 견적(`ScopeProposal`)이 한 번이라도 생성되면 일정·주소·상세 주소·사다리차를 포함한 모든
기본정보 수정은 `409 Conflict`로 거부된다. 견적을 무효화하거나 자동으로 재검토 상태로 돌리지
않는다.

프론트엔드는 다음을 적용한다.

1. 조회 결과에 견적이 있으면 기본정보 수정 버튼과 입력을 숨기거나 비활성화한다.
2. 동시 요청 등으로 `PATCH`가 `409`를 반환하면 작업을 다시 조회하고 “견적 생성 후에는 수정할
   수 없습니다”라고 안내한다.
3. 서버 응답이 성공하기 전에 local draft를 확정 상태처럼 표시하지 않는다.

## 4. 현장 이슈 증거 열람

현장 보고 상세나 업체 변경안 화면에서 이슈 목록의 `evidence_media_asset_ids` 각각에 다음 endpoint를
호출한다.

```http
GET /api/v1/move-jobs/{job_id}/field-issues/{field_issue_id}/evidence/{media_asset_id}/read-url
Authorization: Bearer <customer-or-company-or-field-worker-secret>
```

```json
{
  "media_asset_id": "uuid",
  "room_zone_id": "uuid",
  "content_type": "image/jpeg",
  "read_url": "https://opaque-signed-url",
  "expires_at": "2026-08-19T15:00:00Z"
}
```

- 작업에 연결된 고객·업체·현장기사 모두 호출할 수 있다.
- 서버는 참여자 역할, 작업, 이슈, 이슈에 연결된 media, `change_evidence` 목적과 READY generation을
  모두 확인한다.
- 아직 READY가 아니면 `409`, storage 발급 실패는 `503`, 다른 작업·이슈·media는 `404`다.
- URL은 5분 뒤 만료되고 `Cache-Control: no-store`다. 만료 뒤 같은 endpoint를 다시 호출한다.
- `read_url`은 opaque 문자열로 그대로 사용하고 storage·log·analytics에 보관하지 않는다.
- object key와 generation은 응답하지 않는다.

기존 `FieldIssueResponse` 구조는 바뀌지 않았다. 목록의 ID를 사용해 필요한 preview만 별도로
발급하는 추가형 계약이다.

## 5. 확인서 전체 이력

```http
GET /api/v1/move-jobs/{job_id}/scope-review/history
Authorization: Bearer <customer-or-company-secret>
```

응답의 `versions`는 `sequence_number` 오름차순이다. 각 항목은 다음을 포함한다.

- `scope_version_id`, `parent_scope_version_id`, `sequence_number`, `version_label`
- `source`: `scope | quote | field_change`
- 전체 `content`, `content_hash`
- 해당 버전에 생성된 `quote` 또는 `null`
- 업체 합의에서 상속되는 `included_works`, `exclusions`
- `proposal_id`, `proposal_reason`
- `confirmations[]`: `participant_id`, `role`, `confirmed_at`
- `bilaterally_confirmed`, `created_at`, `locked_at`

고객과 업체만 조회할 수 있고 현장기사는 `403`이다. 아직 범위가 없으면 성공 응답의
`versions`가 빈 배열이다. 기존 `GET /scope-versions`와 현재 상태용 `GET /scope-review`는 그대로
유지되므로 기존 소비자를 교체하지 않는다.

## 6. 유지하지만 현재 FE에 추가 연동하지 않는 API

다음 기능은 삭제하거나 축소하지 않는다. 현재 화면에서만 연동 대상으로 잡지 않는다.

| 기능 | 처리 |
| --- | --- |
| 외부 알림 contact point·Email·SMS·카카오 delivery | 기능 플래그·운영 기능으로 유지, 설정 UI 추가 보류 |
| 완료 문서 ZIP | API 유지, 현재 FE 버튼 추가하지 않음 |
| 전체 감사 로그 | 운영·문제 추적용 유지 |
| background job 조회·재시도 | 서버 운영 기능으로 유지 |
| access link 직접 회전·철회 | 보안·운영 기능으로 유지 |
| `POST /move-jobs` 세 역할 bootstrap | 테스트·신뢰 운영용 유지; 고객 생성은 onboarding 사용 |
| 예정일 필터, 계단·운반거리, 작업 제목 수정 | 계약 유지; 현재 화면 입력·필터 추가 불필요 |
| 완료 요청 철회 | 계약 유지; 현재 FE 버튼 추가 불필요 |
| `change-requests` | 호환 소비자 확인 전 삭제 금지; 현재 FE는 `field-issues/change-proposals` 사용 |
| `completion-confirmations` | 호환 소비자 확인 전 삭제 금지; 현재 FE는 완료 요청·결정 사용 |
| 범용 scope approval | 호환·운영용 유지; 현재 FE는 `scope-review/confirm` 사용 |

deprecated 표시는 실제 소비자를 확인한 뒤 별도 계약 변경으로 검토한다.

## 7. FE 연동 체크리스트

1. onboarding 생성 payload에 `detail_address`와 `conditions.ladder`를 보낸다.
2. 고객 기본정보는 GET 응답을 원본으로 편집하고 PATCH 성공 뒤 다시 조회한다.
3. 목록은 `next_cursor`가 `null`이 될 때까지 순회하고 검색·필터 변경 시 cursor를 폐기한다.
4. 견적이 있으면 기본정보 편집을 막고 동시 `409`에서는 서버 상태를 다시 조회한다.
5. 업체 변경안 작성 전 이슈별 evidence ID로 만료 preview를 발급한다.
6. 확인서 기록 탭은 `scope-review/history`의 버전·견적·확인 시각을 그대로 표시한다.
7. access secret, CSRF token과 signed URL은 기존 보안 원칙대로 영구 저장소·URL query·log에 남기지
   않는다.
