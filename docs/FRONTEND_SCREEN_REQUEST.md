# SEQRET MVP 추가 화면 요청서

> 수신: 프론트엔드·제품 디자인
>
> 요청일: 2026-08-12
>
> 우선순위: MVP end-to-end flow 완성

## 1. 요청 배경

현재 [MVP 와이어프레임](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/)에는 다음 8개 업무 화면이 있다.

- [고객 3개](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/?node-id=88-12): 작업범위 확인, 사진·AI 검토, 현장 변경 승인
- [업체 3개](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/?node-id=88-176): 작업범위 검토·확정, 배차·인력 배정, 완료·변경 내역
- [현장기사 2개](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/?node-id=88-691): 현장 상세·체크인, 변경·이슈 보고

현재 화면만으로는 다음 업무 흐름이 끝까지 연결되지 않는다.

1. 현장기사가 작업을 끝낸 뒤 완료 사진·체크리스트·현장 확인을 입력할 수 없다.
2. 업체가 완료 확인 요청을 보내도 고객이 확인하거나 문제를 신고할 화면이 없다.
3. 현장기사의 이슈 보고를 업체가 검토하고 금액이 있는 변경안으로 만드는 상태가 없다.

관련 backend contract는 [API 명세](API_SPEC.md)를 기준으로 한다.

## 2. 요청 범위 요약

| 우선순위 | 구분 | 요청 항목 | 구현 방식 |
| --- | --- | --- | --- |
| P0 | 신규 화면 | 현장기사 `작업 완료 기록` | mobile 전체 화면 1개 |
| P0 | 신규 화면 | 고객 `완료 확인` | mobile 전체 화면 1개 |
| P0 | 기존 화면 variant | 업체 `현장 이슈 견적` | 기존 작업범위 검토 화면 재사용 |
| P0 | bottom sheet | 고객 범위 수정 요청 사유 | 기존 고객 범위 화면에서 열기 |
| P0 | bottom sheet | 고객 변경 설명 요청·거절 | 기존 변경 승인 화면에서 열기 |
| P1 | 공통 상태 | 링크 오류, AI 처리, upload 실패, 배차 충돌 | 기존 화면 variant |
| 결정 필요 | 조건부 화면 | 최초 사진 upload·AI 제출 | 실제 사용자 upload이면 추가 |
| 결정 필요 | 조건부 화면 | 작업 생성·고객 초대 | admin seed가 아니면 추가 |

새 design system이나 별도 dashboard는 요청하지 않는다. 기존 [디자인 시스템·스타일 가이드](https://www.figma.com/design/5O1rDwIOxzdb0iW8Aa5K5m/?node-id=156-26)의 component와 token을 재사용한다.

## 3. P0 신규 화면 1 — 현장기사 작업 완료 기록

### 목적

업체 `완료·변경 내역` 화면에 표시되는 완료 사진, 체크리스트, 현장 확인과 실제 종료 정보를 현장기사가 생성한다.

### 진입 조건

- 배차가 확정된 현장기사 link로 접근한다.
- `현장 도착 체크인` 이후에만 활성화한다.
- 완료되지 않은 현장 이슈가 있으면 상단에 경고하되 기록 작성은 막지 않는다.

### 화면 구성

| 영역 | 표시·입력 항목 |
| --- | --- |
| 작업 header | 작업 code, 출발·도착 요약, 체크인 시각, 최신 승인 범위 version |
| 완료 사진 | backend가 요구한 공간별 사진 group, upload 수, 추가·삭제·재시도 |
| 완료 체크리스트 | backend가 내려준 필수 항목, 완료 여부, 미완료 사유 |
| 현장 변경 요약 | 승인·거절·처리 중인 현장 변경 목록 |
| 작업자 근무 기록 | 배정 작업자, 시작·종료 시각, 실제 근무시간 확인 |
| 고객 현장 확인 | 고객 현장 확인 또는 서명, 확인 시각 |
| 제출 안내 | 제출 후 완료 기록은 감사 이력에 남고 임의 수정할 수 없다는 안내 |

### 화면 행동

- 사진 추가·삭제·upload 재시도
- 체크리스트 완료 표시
- 실제 종료 시각과 작업자별 근무 기록 확인
- 고객 현장 확인 또는 서명 받기
- Primary CTA 권장 문구: `작업 완료 기록 제출`

### 제출 제한

- backend가 지정한 필수 사진 group이 모두 충족되어야 한다.
- 필수 체크리스트가 모두 완료되어야 한다.
- 고객 현장 확인이 있어야 한다.
- 현장기사는 최종 금액과 변경 승인 결과를 수정할 수 없다.

### 필요한 상태

- 사진 upload 중·성공·실패·재시도
- 필수 항목 미완료로 CTA disabled
- 중복 제출 시 기존 완료 기록 표시
- offline 또는 API 실패 후 입력 내용 유지
- 제출 완료 success state

### API 연결

- 기존 `GET /api/v1/move-jobs/{job_id}/field-brief`에 완료 checklist와 배정 인력 추가
- 기존 `POST /api/v1/move-jobs/{job_id}/media-uploads`의 `purpose`에 `completion` 추가
- 신규 `POST /api/v1/move-jobs/{job_id}/completion-submissions`

## 4. P0 신규 화면 2 — 고객 완료 확인

### 목적

업체가 보낸 완료 확인 요청을 고객이 검토하고 확인하거나 문제를 신고한다.

### 진입 조건

- 업체의 `완료 확인 요청 보내기`가 성공한 뒤 발송되는 외부 알림 deep link로 진입한다.
- 고객 access link로만 조회한다.

### 화면 구성

| 영역 | 표시 항목 |
| --- | --- |
| 작업 header | 작업 code, 완료 시각, 승인 범위 version |
| 최종 금액 | 기본 합의금, 승인된 현장 변경, 최종 금액 |
| 완료 사진 | 공간별 완료 사진 preview와 전체 보기 |
| 작업 완료 정보 | 체크리스트 완료 수, 실제 작업시간, 고객 현장 확인 여부 |
| 현장 변경 | 변경 제목, 사유, 승인 시각, 증감 금액 |
| 기록 안내 | 확인 시각과 결과가 감사 기록으로 보존된다는 안내 |

### 화면 행동

- 완료 사진 보기
- Primary CTA: `완료 확인`
- Secondary action: `문제 신고`

`문제 신고`는 같은 화면의 bottom sheet로 처리한다.

- 유형: `작업 누락`, `파손`, `금액`, `기타`
- 상세 내용: 필수, 1..2000자
- MVP에서는 추가 증빙 upload를 요청하지 않는다.

### 필요한 상태

- 확인 요청 만료·철회
- 이미 확인한 요청
- 문제 신고 접수 완료
- 완료 사진 일부 load 실패
- 최종 범위나 금액 data load 실패

### API 연결

- 기존 `GET /api/v1/move-jobs/{job_id}/completion-summary`를 고객 role에도 제공
- 신규 `POST /api/v1/move-jobs/{job_id}/completion-requests/{request_id}/decision`
- decision: `confirm` 또는 `report_issue`

## 5. P0 기존 화면 variant — 업체 현장 이슈 견적

### 구현 방식

새 전체 화면을 만들지 않고 기존 업체 `작업범위 검토·확정` 화면을 `현장 이슈 견적` mode로 재사용한다.

### 진입 조건

- 현장기사의 `업체에 이슈 보고` 이후 외부 알림 deep link로 진입한다.
- link에 `field_issue_id`를 포함한다.

### 기존 화면에서 달라지는 영역

| 영역 | 현장 이슈 mode 내용 |
| --- | --- |
| 상단 요약 | 이슈 유형, 보고 시각, 작업 일시 중지 여부 |
| 증빙 | 현장기사 설명과 증빙 사진 |
| 기준 범위 | 현재 잠긴 scope version과 기존 합의금 |
| 변경 편집 | 추가·제외 항목, 수량·작업 변경 |
| 금액 편집 | 증감 사유와 원 단위 금액 |
| 결과 요약 | 기존 금액, 증감 금액, 변경 후 총액 |

### 화면 행동

- 현장기사에게 추가 확인이 필요하면 기존 전화·업체 chat deep link 사용
- Primary CTA 권장 문구: `변경안 고객에게 보내기`
- 현장기사가 보고한 값은 evidence로만 표시하고 업체가 금액을 확정한다.

### 필요한 상태

- 이미 처리된 이슈
- 다른 업체 담당자가 먼저 제안한 충돌
- 증빙 사진 load 실패
- 기준 scope version이 바뀐 stale 상태
- 작업 일시 중지 경고

### API 연결

- 기존 `GET /api/v1/move-jobs/{job_id}/scope-review`에 `source_field_issue_id` query 또는 동등한 mode 입력 추가
- 기존 `POST /api/v1/move-jobs/{job_id}/scope-proposals`의 `source_field_issue_id` 사용

## 6. P0 bottom sheet

### 6.1 고객 범위 수정 요청

진입: 고객 `작업범위 확인` 화면의 `수정 요청`.

- 제목: `어떤 내용을 수정할까요?`
- 입력: 수정 사유 1..2000자
- 취소, `수정 요청 보내기`
- 공백 제출 금지
- 성공 후 화면 status를 `수정 요청됨`으로 변경
- API: `POST /scope-review/revision-request`

### 6.2 고객 변경 설명 요청·거절

진입: 고객 `현장 변경 승인` 화면의 `설명 요청 또는 거절`.

- 행동 선택: `설명 요청`, `거절`
- note 1..2000자 필수
- 선택한 행동에 맞춰 CTA 문구 변경
- 성공 후 변경 상태와 처리 시각 표시
- API: `POST /change-proposals/{proposal_id}/decision`

## 7. 기존 화면에 필요한 상태 variant

| 대상 화면 | 추가 상태 | 요구사항 |
| --- | --- | --- |
| 공통 link 진입 | 만료, 철회, 잘못된 link | 이유별 민감정보를 노출하지 않고 재요청 안내 |
| 고객 사진·AI 검토 | 분석 중 | 진행 안내와 재접속 가능 안내; CTA disabled |
| 고객 사진·AI 검토 | 분석 실패 | 수동 작업 가능 안내와 업체 문의 action |
| 현장기사 이슈 보고 | upload 실패 | 실패 사진만 재시도하고 작성 text 유지 |
| 업체 배차·인력 | 차량 충돌, 작업자 충돌, 자격 미충족 | 문제 항목 강조, CTA disabled, 대체 후보 유지 |
| 업체 완료·변경 내역 | 문서 준비 중·실패 | ZIP download disabled, 재시도 안내 |
| 모든 mutation | 중복 탭·처리 중 | CTA loading과 중복 제출 차단 |

## 8. 진행 표시와 알림 정리 요청

### 역할 화면의 `1/3`, `2/3` 표시

고객의 현장 변경 승인과 현장기사의 이슈 보고는 항상 거치는 선형 단계가 아니다. 따라서 분수형 progress는 실제 흐름과 맞지 않는다.

권장:

- 고객·현장기사 mobile 화면의 `1/3`, `2/3`, `3/3` 제거
- 필요하면 `사전 확인`, `현장 작업`, `완료` 같은 phase label 사용
- 업체 desktop의 실제 workflow sidebar는 유지

### 업체 header의 `알림 3`

현재 알림 목록 화면이 없다. MVP 권장안은 다음과 같다.

- `알림 3`을 제거한다.
- 고객 확인·현장 이슈·완료 요청은 외부 알림 deep link로 해당 화면을 직접 연다.
- 알림 count를 유지하려면 별도 알림함 화면과 읽음 처리가 추가 범위가 된다.

## 9. 조건부 추가 화면

다음 두 항목은 frontend 구현 전에 제품 범위를 확인해 달라.

### 9.1 최초 사진 upload·AI 제출

현재 고객 화면은 사진과 AI 결과가 이미 준비된 상태에서 시작한다.

- 사진을 seed/admin 과정에서 준비하면 추가 화면이 필요 없다.
- 고객이나 업체가 실제로 촬영·upload해야 하면 공간 선택, 사진 upload, 제출과 AI 처리 대기 화면이 필요하다.

추가 시 API 영향:

- `media-uploads.purpose`에 `inventory`, `condition` 추가
- `POST /api/v1/move-jobs/{job_id}/capture-submissions` 추가

### 9.2 작업 생성·고객 초대

현재 업체 화면은 `고객 정보 완료` 이후부터 시작한다.

- 작업과 access link를 admin이 미리 만들면 추가 화면이 필요 없다.
- 업체가 직접 작업을 만들고 고객 link를 공유해야 하면 작업 생성·고객 정보·초대 link 공유 화면이 필요하다.

## 10. 이번 요청에서 제외

- 결제와 현금영수증 발행 화면
- 차량·작업자 master 등록·수정 화면
- 전체 감사 이력 화면
- background job·정합성·삭제 운영 화면
- 별도 알림함과 설정 화면
- 자체 전화·채팅·지도 화면
- AI provider 세부 상태와 prompt 관리 화면

## 11. Figma 전달물 요청

각 항목에 다음 내용을 포함해 달라.

- 신규 화면 또는 기존 frame의 named variant
- mobile은 현재 440×880, desktop은 현재 1440×900 기준
- normal, loading, empty, error, disabled, success 상태
- CTA와 secondary action의 정확한 문구
- deep link 진입, 뒤로 가기와 성공 후 이동 위치
- 입력 필수·선택, 글자 수와 사진 수 제한 annotation
- 기존 8개 화면과 신규 화면 사이 prototype 연결
- API field와 연결되는 표시값·입력값 annotation

## 12. 완료 기준

- 현장기사 완료 기록 → 업체 완료 요약 → 고객 완료 확인이 끊기지 않는다.
- 현장기사 이슈 보고 → 업체 견적 → 고객 결정이 끊기지 않는다.
- 모든 CTA가 성공·실패·중복 처리 상태를 가진다.
- 선택 행동에 필요한 사유 입력 UI가 존재한다.
- optional event 화면을 선형 progress로 오해하지 않는다.
- 화면에 없는 결제·운영·관리 기능을 새로 추가하지 않는다.
