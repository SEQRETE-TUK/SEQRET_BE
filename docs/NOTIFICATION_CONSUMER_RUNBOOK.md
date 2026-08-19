# INT-09 notification consumer·외부 전달 운영 절차

## 런타임 계약

기존 `seqret-<env>-relay` Cloud Run Job이 Outbox 발행 뒤 `seqret-<env>-notify` pull
subscription에서 한 번에 최대 100개를 읽는 event pump다. 기존 Cloud Scheduler가 매분 한 번
실행한다. 각 메시지는 엄격한 `DomainEvent` JSON 및 Pub/Sub attribute 검증을 통과한 뒤 하나의
DB transaction에서 `event_consumption` receipt와 `PENDING` in-app notification을 저장한다.
commit이 끝난 뒤에만 ack한다.

parse, DB, ack 중 하나라도 실패하면 ack deadline을 0으로 변경해 nack한다. Pub/Sub는
10초~10분 exponential backoff로 약 5회 전후(best-effort) 전달한 뒤 31일 보존 DLQ topic과
`seqret-<env>-notify-dlq-inspect` subscription으로 보낸다. 외부 발송은 destination 계약이
있는 recipient에 한해 같은 relay 실행의 다음 단계에서 처리한다.

source ack deadline은 순차 100개 DB transaction을 허용하는 240초 Job timeout보다 긴
300초다. nack는 이 deadline을 기다리지 않고 즉시 retry를 요청한다. source
subscription의 oldest unacked event가 15분을 넘은 상태가 5분 지속되면 native
`oldest_unacked_message_age` alert가 열리며, 15분 rate limit과 30분 auto-close를 사용한다.

## 외부 Email·SMS·알림톡 전달

consumer가 event receipt와 in-app intent를 commit할 때 recipient의 active workspace contact를
snapshot한다. 동의한 연락처가 없으면 외부 row를 만들지 않는다. 연락처 변경·철회는 이미 보낸
row를 삭제하지 않고 그 연락처의 아직 `PENDING`인 row를 `FAILED/consent_revoked`로 끝낸다.

외부 발송이 활성화된 relay는 consumer 뒤에 due delivery를 provider 호출 직전에 한 건씩 lease하고
최대 10건을 동시에 NHN Cloud Email v2.0, SMS v3.0 또는 알림톡 v2.2로 보낸다. 한 실행의 상한은
`SEQRET_NOTIFICATION_BATCH_SIZE`이며 개별 timeout은
`SEQRET_NOTIFICATION_DELIVERY_TIMEOUT_SECONDS`, lease는 timeout보다 1초를 초과해 길어야 한다.
재시도 가능 오류는 1초부터 지수 backoff하고 최대 5회 시도한다. 영구 오류와 마지막 실패는
provider 원문 대신 `invalid_input|unavailable|deadline_exceeded|permission_denied` 같은 정제된
분류만 저장한다.

provider 요청·응답 계약은 [Email v2.0](https://docs.nhncloud.com/ko/Notification/Email/ko/api-guide-v2.0/),
[SMS v3.0](https://docs.nhncloud.com/ko/Notification/SMS/ko/api-guide/),
[알림톡 v2.2](https://docs.nhncloud.com/ko/Notification/KakaoTalk%20Bizmessage/ko/alimtalk-api-guide-v2.2/)
공식 문서를 기준으로 한다.

활성화 전에 다음을 모두 준비한다.

- NHN Cloud Email app key·secret과 등록 발신 주소·표시명
- NHN Cloud SMS app key·secret과 등록된 국내 발신번호
- NHN Cloud 알림톡 app key·secret, 40자 발신 프로필 키와 `#{message}`, `#{deepLink}` 변수가
  승인된 템플릿
- 세 provider secret의 기존 Secret Manager secret ID와 relay service account의 accessor 권한
- 정확한 `SEQRET_FRONTEND_ORIGIN`과 `SEQRET_NOTIFICATION_DELIVERY_ENABLED=true`

기본값은 비활성이다. 설정 일부만 넣은 상태에서 활성화하면 application/Terraform/workflow 검증이
fail-closed한다. 먼저 비활성 상태로 schema와 세션·연락처 API를 배포하고, provider 등록과
Secret을 확인한 다음 별도 rollout에서 활성화한다.

전달 의미는 at-least-once다. 알림톡 idempotency key의 provider 보장은 10분이며 Email·SMS
grouping key는 추적 값이다. provider가 수신했지만 응답을 잃은 timeout은 재시도 후 중복 수신이
가능하므로 `notification_delivery.id`, `provider_message_id`와 NHN 발송 결과를 함께 대조한다.
연락처·message body·secret은 로그나 incident 문서에 복제하지 않는다.

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
  `pulled`·`acknowledged`·`notification_failed`, `external_notification_claimed`·`sent`·
  `retry_scheduled`·`failed` count 또는 기존 relay Cloud Run Job failure alert를 먼저 확인한다.
  로그에는 payload, participant, destination, credential을 기록하지 않는다.
- 초기화가 오래 대기하면 source backlog-age alert와 subscription label을 확인한다. `pending`이면
  위 최초 초기화 절차를 따르고, `initializing`이면 Audit Logs 확인 전 seek를 반복하지 않는다.
- DLQ backlog alert가 열리면 inspection subscription에서 메시지를 ack하지 않고 envelope와
  DB aggregate 존재 여부를 확인한다. Pub/Sub가 원본 메시지를 새 DLQ 메시지의 data로
  감싸므로 source subscription attribute와 원본 envelope를 구분해서 확인한다.
- 원인을 고친 뒤 감싸진 원본의 data와 원래 네 routing attribute(`event_id`, `event_type`,
  `schema_version`, `idempotency_key`)만 source topic으로 명시적으로 재게시한다. source receipt가
  이미 commit된 이벤트는 재전달되어도 notification을 중복 생성하지 않는다.
- DLQ 메시지를 확인·재게시·ack한 event ID와 담당자를 incident 기록에 남긴다.
- 외부 `FAILED`가 생기면 `last_error_code`, attempt 수, 해당 배포의 설정 유무와 NHN console의
  request ID만 확인한다. `permission_denied` 또는 `invalid_input`은 자동 재시도하지 않으므로
  발신자·템플릿·수신처 계약을 고친 뒤 새 업무 event로 검증한다. DB row를 수동으로 `PENDING`으로
  바꾸지 않는다.

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

staging 활성화 후에는 동의한 테스트 계정으로 future event를 하나 만들고 in-app과 선택한 외부
채널이 각각 한 row인지, relay 결과가 `sent=1`, provider request ID가 존재하는지, 실제 테스트
수신처가 한 번 받았는지 확인한다. 실제 provider credential이 없는 로컬·CI mock 성공은 운영 발송
증거가 아니다.
