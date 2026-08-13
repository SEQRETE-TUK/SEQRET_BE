# A-09 notification consumer 운영 절차

## 런타임 계약

기존 `seqret-<env>-relay` Cloud Run Job이 Outbox 발행 뒤 `seqret-<env>-notify` pull
subscription에서 한 번에 최대 100개를 읽는 event pump다. 기존 Cloud Scheduler가 매분 한 번
실행한다. 각 메시지는 엄격한 `DomainEvent` JSON 및 Pub/Sub attribute 검증을 통과한 뒤 하나의
DB transaction에서 `event_consumption` receipt와 `PENDING` in-app notification을 저장한다.
commit이 끝난 뒤에만 ack한다.

parse, DB, ack 중 하나라도 실패하면 ack deadline을 0으로 변경해 nack한다. Pub/Sub는
10초~10분 exponential backoff로 약 5회 전후(best-effort) 전달한 뒤 31일 보존 DLQ topic과
`seqret-<env>-notify-dlq-inspect` subscription으로 보낸다. 외부 발송은 destination 계약이
없으므로 이 런타임 범위가 아니다.

source ack deadline은 순차 100개 DB transaction을 허용하는 240초 Job timeout보다 긴
300초다. nack는 이 deadline을 기다리지 않고 즉시 retry를 요청한다. source
subscription의 oldest unacked event가 15분을 넘은 상태가 5분 지속되면 native
`oldest_unacked_message_age` alert가 열리며, 15분 rate limit과 30분 auto-close를 사용한다.

## 최초 31일 replay

Terraform은 topic 보존 기간과 source subscription 보존 기간을 각각 31일로 설정한다. 새
subscription은 Terraform이 관리하는 `replay_contract=v1`과
`seqret_replay_state=pending` label로 생성된다. 해당 replay state map key만 Terraform
`ignore_changes` 대상으로 두므로 초기화 workflow가 바꾼 상태를 이후 apply가 되돌리지
않으며, 나머지 관리 label의 drift는 숨기지 않는다.

event pump는 subscription의 topic과 `replay_contract=v1`이 정확히 일치하고
`seqret_replay_state=complete`일 때만 pull한다. `pending`과 `initializing`은 정상 bootstrap
no-op이므로 Outbox 발행 결과만으로 실행을 끝낸다. label 누락·알 수 없는 상태 또는
topic/contract 불일치는 fail-closed한다. 따라서 subscription 생성 직후 Scheduler가 실행되어도
과거 backlog를 건드리지 않는다.

staging 최초 배포 후 다음 절차를 딱 한 번 실행한다.

1. GitHub의 protected `staging` environment에서 `Initialize staging notification replay`
   workflow를 현재 `main` commit으로 실행한다.
2. 입력값에 `INITIALIZE_31_DAY_REPLAY`와 직전 main 배포가 출력한 immutable container
   digest를 정확히 적는다. workflow는 seek 전에 Job template digest를 대조한다.
3. workflow는 일반 deploy/rollback과 같은 `deploy-staging` concurrency group에서 실행된다.
4. subscription/topic/contract 및 replay state `pending`을 확인하고 state를 `initializing`으로
   바꾼 뒤 현재 시각의 31일 전으로 seek한다.
5. seek가 성공한 경우에만 state를 `complete`로 바꾸고 relay event-pump Job smoke test를 실행한다.
   workflow는 seek 직전에 Job template의 exact digest를 검증하고 `--wait` 실행이 성공해야 끝난다.

`initializing` 또는 `complete`에서는 workflow 재실행이 실패한다. label update 자체가 CAS는
아니므로 같은 concurrency group 밖에서 초기화 script를 실행하면 안 된다. deploy service
account에는 다음 권한이 필요하다. 이 PR의 Terraform은 외부 deploy identity를 추정해 권한을
부여하지 않는다.

- `pubsub.subscriptions.get`
- `pubsub.subscriptions.update`
- `pubsub.subscriptions.consume` (timestamp seek)
- `run.jobs.get`
- `run.jobs.run`
- `run.executions.get`

seek 이후 completion label update만 실패한 것이 workflow log로 확인되면 seek를 다시
실행하지 말고 Cloud Audit Logs에서 성공을 확인한 뒤 label만 `complete`로 바꾼다. seek 성공
여부가 불명확하거나 state가 `initializing`이면 자동 복구하지 말고 Pub/Sub Audit Logs를 먼저
확인한다. event receipt가 중복 notification 생성을 막지만, 중복 replay 자체를 정상 복구
절차로 사용하지 않는다.

state가 이미 `complete`인 뒤 smoke test만 실패했다면 초기화 workflow를 다시 실행하지 않는다.
같은 digest의 Job을 수동 실행해 성공 여부를 확인하고 실패 execution을 조사한다.

## 장애 및 DLQ

- `outbox_relay_complete`의 `outcome=error`와 `claimed`·`published`·`relay_failed` 및
  `pulled`·`acknowledged`·`notification_failed` count 또는 기존 relay Cloud Run Job failure
  alert를 먼저 확인한다. 로그에는 payload, participant, credential을 기록하지 않는다.
- 초기화가 오래 대기하면 source backlog-age alert와 subscription label을 확인한다. `pending`이면
  위 최초 초기화 절차를 따르고, `initializing`이면 Audit Logs 확인 전 seek를 반복하지 않는다.
- DLQ backlog alert가 열리면 inspection subscription에서 메시지를 ack하지 않고 envelope와
  DB aggregate 존재 여부를 확인한다. Pub/Sub가 원본 메시지를 새 DLQ 메시지의 data로
  감싸므로 source subscription attribute와 원본 envelope를 구분해서 확인한다.
- 원인을 고친 뒤 감싸진 원본의 data와 원래 네 routing attribute(`event_id`, `event_type`,
  `schema_version`, `idempotency_key`)만 source topic으로 명시적으로 재게시한다. source receipt가
  이미 commit된 이벤트는 재전달되어도 notification을 중복 생성하지 않는다.
- DLQ 메시지를 확인·재게시·ack한 event ID와 담당자를 incident 기록에 남긴다.

DLQ 담당 operator에는 inspect subscription의 `pubsub.subscriptions.get`과
`pubsub.subscriptions.consume`, source topic의 `pubsub.topics.publish`가 필요하다. 알려지지 않은
operator identity에는 Terraform으로 권한을 미리 부여하지 않는다.

## 검증

로컬/CI에서는 다음을 실행한다.

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
terraform -chdir=infrastructure/terraform fmt -check -recursive
terraform -chdir=infrastructure/terraform validate
terraform -chdir=infrastructure/terraform test
```
