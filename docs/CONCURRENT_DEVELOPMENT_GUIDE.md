# 동시 개발 및 main 머지 충돌 방지 가이드

> 상태: 필수 개발 지침
>
> 기준일: 2026-08-12
>
> 적용 대상: SEQRET Backend에서 작업하는 A·B 담당자와 모든 AI agent

## 1. 목적

이 문서는 A와 B가 동시에 개발해도 main에 작은 단위로 안전하게 병합할 수 있도록 작업 격리, 파일 소유권, 계약, migration, PR과 충돌 해결 절차를 정의한다.

목표는 충돌을 빠르게 해결하는 것이 아니라 다음 세 종류의 충돌을 작업 시작 전에 예방하는 것이다.

- **텍스트 충돌:** 같은 파일의 같은 줄을 동시에 수정하는 문제
- **구조 충돌:** migration, router, dependency와 설정처럼 병합 후 하나의 구조만 유효한 문제
- **의미 충돌:** 코드는 병합되지만 상태 전이, 권한, event와 재시도 의미가 서로 달라지는 문제

## 2. 변경 불가능한 기본 규칙

1. `main`은 항상 배포 가능한 상태로 유지한다.
2. `main` 직접 push를 금지하고 모든 변경은 PR로 병합한다.
3. 한 작업자 또는 AI agent는 하나의 독립 branch와 worktree를 사용한다.
4. 사람별 장기 branch가 아니라 작업 ID별 짧은 branch를 사용한다.
5. 공용 계약은 구현보다 먼저 main에 병합한다.
6. 다른 트랙의 ORM model과 repository를 직접 접근하지 않는다.
7. Alembic history는 단일 head와 선형 구조를 유지한다.
8. 상대방의 미커밋 변경 또는 미병합 branch에 의존하지 않는다.
9. conflict marker가 없다는 이유만으로 의미 충돌이 해결됐다고 판단하지 않는다.
10. AI agent는 자동으로 범위를 확장하거나 다른 작업자의 변경을 정리하지 않는다.

## 3. 작업 소유권

세부 역할과 작업 ID는 [`BACKEND_WORK_SPLIT.md`](BACKEND_WORK_SPLIT.md)를 기준으로 한다.

| 영역 | 주 소유자 | 상대 트랙의 접근 방식 |
| --- | --- | --- |
| 업무 API, 권한, 상태 전이 | A | 공개 application service 호출 |
| SQLAlchemy 공통 기반과 Alembic 순서 | A | B model migration은 A 승인 필요 |
| 촬영 세션과 `media_asset` | A | B는 ID와 Port만 사용 |
| 범위 버전, 확인, 변경, 완료, 감사 | A | B 직접 변경 금지 |
| `background_job`과 task 정책 | A | B handler는 결과 command 호출 |
| Outbox, Pub/Sub, 알림 | A | B는 versioned event 계약 사용 |
| Redis, 공통 관측성, Terraform, CI/CD | A | B는 필요한 signal·resource 요구사항 제공 |
| GCS adapter | B | A는 `StoragePort` 사용 |
| Cloud Tasks adapter와 worker runtime | B | A는 `TaskQueuePort` 사용 |
| Vertex AI adapter와 AI 분석 | B | A는 `AnalysisResult` 계약 사용 |
| 미디어 처리와 물리 삭제 handler | B | A가 대상과 정책 결정 |

## 4. branch와 worktree 격리

### 4.1 branch 이름

```text
feat/a-04-scope-version
feat/b-01-gcs-adapter
fix/a-05-approval-race
refactor/b-02-task-adapter
docs/concurrent-development-guide
```

- 작업 ID가 있는 기능은 branch와 PR 제목에 같은 ID를 사용한다.
- `develop`, `track-a`, `track-b` 같은 장기 통합 branch를 만들지 않는다.
- 하나의 branch에 여러 작업 ID를 섞지 않는다.

### 4.2 AI와 worktree

같은 컴퓨터에서 두 AI 작업을 병렬 실행할 때는 반드시 별도 worktree를 사용한다.

```bash
git fetch origin
git worktree add ../SEQRET_BE-a-a04 -b feat/a-04-scope-version origin/main
git worktree add ../SEQRET_BE-b-b01 -b feat/b-01-gcs-adapter origin/main
```

금지:

- 두 AI가 같은 working directory 사용
- 두 AI가 같은 branch 사용
- 한 AI가 다른 AI의 worktree에서 formatting·테스트 수정
- 다른 branch의 미커밋 파일을 복사해 의존성 해결

### 4.3 작업 시작 선언

작업자는 코드를 수정하기 전에 다음을 PR 설명 또는 작업 기록에 남긴다.

```text
Task ID:
Track:
Base commit:
수정 예정 디렉터리:
수정이 필요한 공용 파일:
Migration 여부:
Port/API/Event 변경 여부:
선행 PR과 예상 merge 순서:
```

두 작업이 같은 공용 파일을 수정할 예정이면 동시에 시작하지 않는다. 공통 변경을 선행 PR로 분리하거나 merge 순서를 먼저 정한다.

## 5. 충돌 위험 파일

다음 파일과 영역은 기본 조정자 A가 merge 순서를 관리한다.

| 경로 또는 파일 | 규칙 |
| --- | --- |
| `pyproject.toml`, lockfile | 한 시점에 하나의 PR만 변경한다. 가능하면 dependency PR로 분리한다. |
| `alembic/env.py`, versions | A가 단일 head와 merge 순서를 관리한다. |
| `app/main.py`, root router | 각 모듈이 router를 노출하고 A가 최종 wiring한다. |
| 공통 DB metadata·session | 별도 foundation PR로 변경한다. |
| `ActorContext`, error model | 계약 PR을 먼저 병합한다. |
| Port와 event envelope | 구현 전에 versioned 계약 PR을 병합한다. |
| `.github/workflows/**` | 기능 PR과 분리하거나 A가 변경한다. |
| `infrastructure/**` | A가 변경하고 B는 resource 요구사항만 제안한다. |
| OpenAPI·생성 파일 | 원본 계약 병합 후 한 PR에서 재생성한다. |

다음 변경은 기능 PR에 섞지 않는다.

- 저장소 전체 formatting
- 광범위한 import 정리
- 공통 파일 rename 또는 이동
- 관련 없는 dependency upgrade
- 다른 트랙의 테스트 일괄 수정

## 6. 계약 우선 개발

A와 B가 연결되는 기능은 다음 순서로 진행한다.

```text
1. 계약 PR
2. fake와 contract test
3. A 업무 오케스트레이션
4. B provider adapter
5. 통합 테스트
```

예시:

```text
StoragePort 계약 merge
  ├─ A: MediaAsset·권한·업로드 API 구현
  └─ B: GCS adapter 구현

AnalysisResult 계약 merge
  ├─ A: ImportAnalysisDraft 구현
  └─ B: Vertex AI 분석 구현
```

규칙:

- B는 A의 feature branch에서 직접 분기하지 않는다.
- 필요한 계약이 없으면 50~150줄 수준의 작은 선행 PR로 먼저 만든다.
- 계약 PR에는 입력·출력, 오류, idempotency key, timeout, version과 소유자를 명시한다.
- 임시 호환 코드를 양쪽에 각각 만들지 않는다.
- event payload 변경은 기존 version을 수정하지 않고 새 schema version을 추가한다.

## 7. Alembic migration 규칙

A가 migration coordinator다.

### 생성 전

1. 열린 migration PR이 있는지 확인한다.
2. 최신 `origin/main`으로 rebase한다.
3. 현재 Alembic head가 하나인지 확인한다.
4. revision 이름에 작업 ID를 넣는다.

예시:

```text
a04_create_scope_version
b03_create_ai_analysis_run
```

### 병합 전

1. 먼저 병합된 migration이 있으면 branch를 rebase한다.
2. `down_revision`을 최신 main head로 연결한다.
3. 빈 DB에서 `upgrade head`를 검증한다.
4. 직전 main schema에서도 `upgrade head`를 검증한다.
5. 가능한 migration은 한 단계 downgrade 후 재-upgrade한다.
6. schema와 data migration이 크면 별도 단계로 분리한다.

금지:

- main에 병합된 migration 수정·삭제
- 두 migration PR 동시 merge
- 충돌을 숨기기 위한 무분별한 merge revision
- migration 파일명만 바꾸고 revision chain을 확인하지 않는 처리
- B가 A의 확인 없이 공통 DB schema를 변경하는 작업

## 8. dependency와 생성 파일

### dependency

- dependency 추가가 두 트랙에 필요하면 별도 선행 PR로 처리한다.
- 기능 branch 두 곳에서 lockfile을 동시에 수정하지 않는다.
- 관련 없는 version upgrade를 기능 구현과 함께 수행하지 않는다.
- dependency 변경 PR에는 도입 이유, 대안, runtime·image 영향과 rollback 방법을 적는다.

### 생성 파일

- OpenAPI, client schema와 lockfile은 수동으로 줄 단위 병합하지 않는다.
- 원본 설정 또는 계약의 충돌을 먼저 해결한 뒤 repository 표준 명령으로 다시 생성한다.
- 생성 결과만 달라지고 원본 변경이 없다면 merge를 중단하고 생성 환경 차이를 확인한다.

## 9. 커밋 메시지와 PR 제목

### 형식

```text
<type>(<Task-ID>): <간결한 한국어 요약>
```

Task ID가 없는 저장소 공통 문서·설정 작업은 다음 형식을 사용할 수 있다.

```text
<type>: <간결한 한국어 요약>
```

허용 type:

| Type | 사용 대상 |
| --- | --- |
| `feat` | 사용자 또는 시스템 기능 추가 |
| `fix` | 결함 수정 |
| `refactor` | 동작 변경 없는 구조 개선 |
| `test` | 테스트 추가·수정 |
| `docs` | 문서 변경 |
| `chore` | 일반 유지보수와 설정 정리 |
| `perf` | 성능 개선 |
| `ci` | CI workflow 변경 |
| `build` | build·dependency·container 변경 |
| `revert` | 이전 변경 되돌림 |

규칙:

- 요약은 구체적인 한국어로 작성한다.
- 전체 제목은 가능하면 50자 이내로 작성한다.
- 제목 끝에 마침표를 붙이지 않는다.
- 이모지와 AI 도구명·모델명을 넣지 않는다.
- `수정`, `업데이트`, `작업`, `변경사항 반영`처럼 대상을 알 수 없는 표현을 단독으로 사용하지 않는다.
- 하나의 commit에는 하나의 목적만 담는다.
- 이유, 위험과 migration 순서가 필요할 때만 빈 줄 뒤에 한국어 본문을 추가한다.
- 호환성을 깨는 변경은 type 뒤에 `!`를 붙이고 본문에 영향과 전환 방법을 적는다.
- Squash Merge를 사용하므로 PR 제목이 최종 commit 메시지가 된다. PR 제목도 같은 규칙을 적용한다.

좋은 예시:

```text
feat(A-04): 작업범위 불변 버전 생성
fix(B-02): 중복 작업 실행 차단
refactor(B-01): 객체 저장소 어댑터 분리
test(A-05): 양측 확인 충돌 검증 추가
docs: 동시 개발 규칙 정리
ci: 백엔드 통합 테스트 단계 추가
```

피해야 할 예시:

```text
update
수정
feat: 여러 기능 추가
chore: AI로 생성한 코드 반영
fix: 이것저것 오류 수정
```

## 10. 동기화와 PR 흐름

### 작업 중

- 최소 하루 한 번 최신 main과 차이를 확인한다.
- 다른 PR이 자신의 공용 파일이나 계약에 영향을 주면 즉시 rebase한다.
- 구현이 끝날 때까지 기다리지 말고 Draft PR을 일찍 연다.
- PR 본문에 수정 경로, migration, 계약, 의존 PR과 merge 순서를 유지한다.

### 리뷰 전과 merge 직전

```bash
git fetch origin
git rebase origin/main
```

- rebase 이후 전체 관련 테스트를 다시 실행한다.
- 리뷰 중인 branch를 force-push해야 하면 `--force-with-lease`만 사용하고 상대 리뷰어에게 알린다.
- main을 feature branch로 merge하는 방식은 사용하지 않는다.

### GitHub merge

- 상대 담당자 승인 1개
- 필수 CI 성공
- 모든 review conversation 해결
- 최신 main 반영
- Squash Merge
- branch 자동 삭제

## 11. 권장 main 보호 설정

GitHub의 main branch에 다음 규칙을 적용한다.

- Pull request 필수
- 승인 1개 이상 필수
- 새 commit 발생 시 기존 승인 무효화
- 필수 status check 통과
- review conversation 해결 필수
- linear history 필수
- force push와 branch 삭제 금지
- 관리자도 규칙 우회 금지
- 가능하면 merge queue 사용

권장 CI:

- formatting/lint
- type check
- unit test
- integration test
- migration 단일 head 검사
- 빈 DB와 직전 schema migration 검사
- module boundary 위반 검사
- container build

## 12. merge 순서 결정

| 충돌 상황 | 먼저 병합 | 후속 처리 |
| --- | --- | --- |
| 공통 Port와 B adapter | 계약 PR | B가 최신 계약 기준으로 adapter PR rebase |
| A schema와 B handler | A schema PR | B가 migration·model 참조 없이 public command 사용 |
| 두 migration PR | 먼저 준비된 하나 | 나머지는 rebase하고 revision chain 재생성 |
| dependency와 기능 | dependency PR | 두 기능 branch 모두 최신 main으로 rebase |
| root router와 모듈 router | 모듈 router | A가 root wiring을 작은 PR로 병합 |
| OpenAPI 계약과 생성 파일 | 원본 계약 | 생성 파일을 한 번만 재생성 |
| Terraform과 worker 요구사항 | B의 요구사항·계약 | A가 Terraform 반영 |

병합 순서가 불분명하면 파일을 먼저 수정한 사람이 아니라 해당 파일과 계약의 소유자가 결정한다.

## 13. 충돌 발생 시 해결 절차

### 1단계 — 자동 선택 금지

파일 전체에 `ours` 또는 `theirs`를 적용하지 않는다. 먼저 충돌을 다음으로 분류한다.

- 트랙 소유 파일
- 공용 계약
- migration
- dependency·lockfile
- 생성 파일
- 테스트 기대값

### 2단계 — 소유자 확인

- A 소유 파일은 A가 의미를 설명하고 해결한다.
- B provider adapter는 B가 해결한다.
- 공용 계약은 두 사람이 함께 결정한다.
- migration은 A가 revision chain을 확인한다.
- lockfile·생성 파일은 원본 충돌을 해결한 뒤 다시 생성한다.

### 3단계 — rebase

```bash
git fetch origin
git rebase origin/main
# 파일별로 의미를 확인해 수정
git add <resolved-file>
git rebase --continue
```

안전하게 판단할 수 없으면 임의로 진행하지 않고 다음으로 돌아간다.

```bash
git rebase --abort
```

### 4단계 — 의미 검증

- 양쪽 테스트가 모두 유지되는지 확인한다.
- 상태 전이, 권한과 idempotency 의미가 바뀌지 않았는지 확인한다.
- migration head와 upgrade 경로를 확인한다.
- Port·event·OpenAPI가 구현과 일치하는지 확인한다.
- conflict marker가 남아 있지 않은지 검사한다.

## 14. AI agent 전용 절차

AI agent는 작업 시작 시 다음 정보를 사용자 또는 상위 작업에 보고한다.

```text
Task ID와 트랙
현재 branch와 base commit
수정할 파일·디렉터리
공용 파일 수정 여부
Migration·Port·API·Event 변경 여부
선행 PR 또는 예상 merge 순서
```

AI agent는 다음 원칙을 지킨다.

- 현재 `git status`와 diff를 읽기 전에는 파일을 수정하지 않는다.
- 다른 작업자의 변경을 정리, 이동, rename 또는 format하지 않는다.
- 작업 범위 밖의 실패를 고치지 않고 별도 위험으로 보고한다.
- 상대 트랙 구현이 없으면 직접 침범하지 않고 fake 또는 Port를 사용한다.
- 충돌 해결 시 문법적으로 합쳐지는 것보다 소유권과 도메인 의미를 우선한다.
- 명시적 요청 없이는 commit, push, PR merge 또는 branch 삭제를 수행하지 않는다.
- 최종 인계에 변경 파일, migration, 계약, 테스트, 미해결 위험과 merge 순서를 적는다.

AI에게 작업을 요청할 때 권장 프롬프트:

```text
AGENTS.md와 docs/CONCURRENT_DEVELOPMENT_GUIDE.md를 먼저 읽어라.
작업 ID는 A-04이며 A 트랙이다.
origin/main 기준의 독립 branch/worktree에서 작업하고,
B 소유 경로와 공용 계약은 수정하지 마라.
필요하면 먼저 계약 변경을 별도 제안하고 구현을 중단하라.
완료 후 변경 파일, 테스트, migration, 계약 영향과 merge 순서를 보고하라.
```

## 15. PR 인계 정보

모든 PR은 최소 다음 내용을 제공한다.

- 작업 ID와 트랙
- 해결한 문제와 범위
- 수정한 소유 영역과 공용 파일
- 선행·후속 PR
- schema·migration 변경
- Port·API·event 변경과 version
- 실행한 테스트
- 재시도·동시성·권한 영향
- 배포·rollback 방법
- 권장 merge 순서
