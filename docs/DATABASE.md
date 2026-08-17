# 데이터베이스 개발 가이드

## Runtime 계약

- 업무 runtime은 `postgresql+psycopg` URL만 허용한다.
- `SEQRET_DATABASE_URL`은 비밀값으로 취급하며 설정 repr, health 응답과 로그에 노출하지 않는다.
- `participant_access_token`의 `rate_window_*` 열은 원자적 fixed-window 원본이며 Redis는 같은 구간의 보조 제한을 적용한다. Redis 장애 뒤에도 같은 DB 구간을 이어 쓰며 평문 역할 링크는 Redis key와 DB 어느 쪽에도 저장하지 않는다.
- `participant_invitation`은 작업별 비고객 역할 하나의 발급자·대상·현재 access-link ID와 `PENDING|ACCEPTED|DECLINED|EXPIRED|REVOKED` 상태만 저장한다. 평문 secret은 저장하지 않으며 하위 초대의 만료는 발급자 access link 만료를 넘지 않는다.
- `scope_proposal`은 source/result 불변 범위 version, 업체 제안자, 원화 견적 snapshot, 포함·제외 작업과 `CUSTOMER_REVIEW|REVISION_REQUESTED|CONFIRMED|SUPERSEDED` 상태를 저장한다. `scope_revision_request`는 고객 요청과 다음 제안에 의한 해결 연결을 보존하며 기존 범위·견적 이력을 덮어쓰지 않는다.
- `field_issue`와 `field_issue_evidence`는 잠긴 기준 범위에 대한 현장기사 보고와 검증된 증거 연결을 보존한다. `change_proposal_detail`은 기존 `change_request`에 이슈·제목·원화 견적 snapshot을 1:1로 연결하며 기존 범위·금액 이력을 덮어쓰지 않는다.
- `dispatch_setup`은 작업별 현재 범위·일정·요구사항·checklist와 차량·인력 후보의 불변 snapshot을 저장한다. `dispatch_plan`은 그 snapshot에서 확정한 차량·인력 ID와 command hash를 작업별 한 건으로 고정하고, `field_check_in`은 배정된 대표 현장기사의 checklist 확인과 당일 도착 시각을 한 번 기록한다.
- `completion_submission`은 현재 배차·범위·대표 기사, 완료 checklist, 작업자 근무 구간과 현장 확인 시각을 불변 command hash와 함께 저장한다. 선택적 `completion_submission_evidence`는 검증된 completion 미디어를 연결한다. `completion_request`는 업체의 7일 고객 확인 요청·철회·결정 상태를 보존하고, `completion_problem_report`는 고객 문제 유형·설명을 요청별 최대 한 건으로 분리한다.
- SQLAlchemy engine은 `pool_pre_ping`과 parameter hiding을 활성화한다.
- application command는 `transactional_session`을 경계로 한 번 commit되며 예외 시 전체 rollback된다.
- 각 ORM model은 `app.platform.db.Base`를 사용해 Alembic constraint 이름을 결정적으로 유지한다.
- `audit_event`는 DB trigger가 일반 application DML의 UPDATE·DELETE를 거부하고 PostgreSQL에서는 TRUNCATE도 거부한다. 현재 API와 migration이 같은 DB owner 자격증명을 쓰므로 accidental mutation 방지선일 뿐, owner가 DDL로 trigger를 비활성화하는 공격까지 막는 권한 경계는 아니다. 별도 application DB role·grant·Secret은 외부 자격증명 준비 후 분리한다.

## Migration

트랙 A가 하나의 선형 Alembic head를 관리한다. 새 revision을 만들기 전에 최신 `origin/main`과 열린 migration PR을 확인한다.

```bash
uv run alembic heads --verbose
uv run alembic revision -m "설명" --rev-id <work_id_revision>
uv run alembic upgrade head
uv run alembic downgrade -1
```

`SEQRET_DATABASE_URL`이 없으면 실제 upgrade와 downgrade는 거부된다. 이미 main에 병합된 revision은 수정하거나 삭제하지 않는다.

현재 단일 head는 `a_19_0001`이다. `location.conditions`는 출·도착지 조건을 값 또는 `unknown`으로 보존하고 `capture_session`은 미디어 동의 정책 버전·확인 여부·보관기간·동의 시각 snapshot을 보존한다. 완료 제출·요청·문제·새 완료 event 또는 사용자 지정 완료 checklist, 배차·체크인·`dispatch_confirmed.v1` 전달 이력, 현장 이슈·변경 제안, 범위 제안·수정요청, 감사·완료 확인, 촬영 분석 제출 또는 참여자 초대 이력이 존재하면 이를 제거하는 schema downgrade는 거부한다. 기본 checklist만 자동 보강된 기존 A-13 setup은 INT-04 history로 간주하지 않아 더 오래된 migration의 자체 guard까지 정상 진행한다. 이 경우 schema를 유지하고 이전 application revision으로 traffic만 전환한다.

첫 baseline revision은 업무 table을 만들지 않고 향후 domain migration이 연결될 단일 head만 고정한다.

## Test database

통합 테스트는 운영 DB가 아닌 이름이 정확히 `seqret_test`인 localhost PostgreSQL만 허용한다. CI와 동일한 DB는 다음처럼 실행할 수 있다.

```bash
docker run --rm --name seqret-postgres-test \
  -e POSTGRES_DB=seqret_test \
  -e POSTGRES_USER=seqret \
  -e POSTGRES_PASSWORD=seqret_test_password \
  -p 5432:5432 \
  postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94
```

```bash
SEQRET_TEST_DATABASE_URL=postgresql+psycopg://seqret:seqret_test_password@127.0.0.1:5432/seqret_test uv run pytest
```

Migration 검증은 다음을 모두 확인한다.

1. revision head가 정확히 하나다.
2. 빈 PostgreSQL DB를 `head`까지 upgrade한다.
3. 기존 schema object를 보존한 채 직전 schema에서 upgrade한다.
4. `base` downgrade 후 동일 head로 다시 upgrade한다.
