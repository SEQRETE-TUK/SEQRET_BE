# AGENTS.md

이 지침은 SEQRET 백엔드 저장소 전체에 적용된다. 사람과 AI 작업자는 코드를 수정하기 전에 반드시 이 문서와 아래 기준 문서를 읽는다.

## 필수 확인 문서

1. `docs/TECH_STACK.md`
2. `docs/BACKEND_WORK_SPLIT.md`
3. `docs/CONCURRENT_DEVELOPMENT_GUIDE.md`

충돌 시 더 구체적인 하위 디렉터리의 `AGENTS.md`가 우선하고, 같은 범위에서는 위 세 문서와 본 문서를 함께 만족해야 한다.

## 작업 시작 전 필수 절차

- 작업 ID와 담당 트랙을 먼저 확정한다: `A-*`, `B-*`, `FND-*`, `INT-*`.
- 현재 branch, base commit, `git status`와 기존 미커밋 변경을 확인한다.
- 한 사람 또는 AI agent마다 별도 branch와 별도 worktree를 사용한다.
- 두 작업자가 같은 branch나 같은 working directory에서 동시에 수정하지 않는다.
- 최신 `origin/main`에서 작업 branch를 만들고, 장기 `develop`, `track-a`, `track-b` branch를 만들지 않는다.
- 수정 예정 경로, 공용 파일, migration, Port·event 변경 여부와 선행 PR을 작업 시작 전에 명시한다.
- 기존 사용자 변경이나 다른 작업자의 변경을 삭제·복원·덮어쓰지 않는다.

## 트랙 경계

### A — Product Core & Platform Orchestration

A는 업무 API, DB, 권한, 상태 전이, 촬영·미디어 정책, 범위 버전, 확인, 변경, 감사, background job, event, 알림, Redis, 공통 관측성과 배포 기반을 소유한다.

### B — AI & Media Processing Integrations

B는 GCS, Cloud Tasks, Vertex AI adapter, AI 분석, 미디어 처리, worker와 media job handler를 소유한다.

### 절대 규칙

- B는 A 소유 ORM model이나 repository를 직접 import하거나 갱신하지 않는다.
- B의 처리 결과는 A가 공개한 application command 또는 versioned event로 전달한다.
- A는 `google.cloud.*`를 도메인·애플리케이션 코드에서 직접 호출하지 않고 Port를 사용한다.
- AI 결과는 확정 범위가 아니며 B는 `scope_version`을 생성·수정·잠금하지 않는다.
- 다른 트랙의 변경이 필요하면 몰래 함께 수정하지 말고 별도 계약 PR 또는 명시적인 공동 작업으로 분리한다.

## 공용 파일 규칙

다음은 충돌 위험이 높은 공용 파일이며 기본 조정자는 A다.

```text
pyproject.toml
lockfile
alembic/env.py
app/main.py
app/config.py
공통 DB metadata와 session
공통 ActorContext와 error model
Port와 event envelope
.github/workflows/**
infrastructure/**
```

- 공용 파일은 동시에 두 branch에서 수정하지 않는다.
- 두 작업에 필요한 공통 변경은 작은 선행 PR로 먼저 main에 병합한다.
- dependency와 lockfile 변경은 기능 변경과 가능하면 분리한다.
- 코드 전체 formatting, import 정리, rename과 광범위한 refactor를 기능 PR에 섞지 않는다.

## 계약 우선 규칙

- Port, event, 공통 Pydantic schema 또는 API 계약을 먼저 별도 PR로 병합한다.
- B는 A의 미병합 branch를 기반으로 구현하지 않고 main에 병합된 계약과 fake를 사용한다.
- 계약 변경에는 schema version, 하위 호환성, consumer 영향과 merge 순서를 PR에 기록한다.
- 상대 트랙 구현을 기다리기 위해 임시로 상대 repository나 table을 직접 접근하지 않는다.

## Alembic 규칙

- A가 migration merge 순서와 단일 Alembic head를 관리한다.
- migration 생성 전 최신 `origin/main`을 기준으로 rebase한다.
- revision 이름에 작업 ID를 포함한다.
- 동시에 여러 migration PR을 병합하지 않는다.
- 먼저 병합된 migration이 있으면 나중 PR은 rebase 후 `down_revision`을 최신 head로 갱신한다.
- 이미 main에 병합된 migration을 수정하거나 삭제하지 않는다.
- 충돌을 피하기 위한 Alembic merge revision은 기본적으로 만들지 않는다. 선형 history를 유지한다.
- 빈 DB upgrade와 직전 main schema upgrade를 모두 검증한다.
- B 소유 모델의 migration도 A의 승인을 받아 병합한다.

## AI 작업 금지 사항

- 명시적 요청 없이 `main`에 직접 commit 또는 push하지 않는다.
- 명시적 요청 없이 다른 작업자의 branch를 rebase, force-push 또는 삭제하지 않는다.
- `git reset --hard`, `git clean -fd`, 전체 경로 checkout·restore로 변경을 제거하지 않는다.
- 충돌 파일 전체에 `ours` 또는 `theirs`를 적용하지 않는다.
- 테스트를 통과시키기 위해 작업 범위 밖의 모듈, migration 또는 계약을 임의로 변경하지 않는다.
- 사용자 또는 다른 작업자의 미커밋 변경을 자신의 변경으로 간주하지 않는다.
- 생성 파일과 lockfile을 손으로 합치지 않는다. 원본 설정을 해결한 뒤 공식 명령으로 다시 생성한다.

## PR과 merge

- 하나의 작업 ID마다 하나의 짧은 branch와 PR을 사용한다.
- Draft PR을 일찍 열고 수정 경로, 공용 파일, migration과 의존 PR을 표시한다.
- 리뷰 요청 전과 merge 직전에 최신 `origin/main`으로 rebase한다.
- 상대 담당자의 승인 1개, 모든 필수 CI와 대화 해결 이후 Squash Merge한다.
- merge 후 branch를 삭제한다.
- 충돌이 계약, migration 또는 공용 파일에 발생하면 자동 해결하지 않고 해당 소유자와 함께 의미를 확인한다.

## 커밋 메시지와 PR 제목

- 형식은 `<type>(<Task-ID>): <간결한 한국어 요약>`을 사용한다.
- Task ID가 없는 저장소 공통 작업만 `<type>: <간결한 한국어 요약>`을 허용한다.
- type은 `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`, `build`, `revert` 중 하나를 사용한다.
- 요약은 구체적인 한국어로 작성하고 가능하면 50자 이내로 제한한다.
- 마침표, 이모지, AI 도구명과 `update`, `수정`, `작업` 같은 모호한 단독 표현을 사용하지 않는다.
- 하나의 commit에는 하나의 목적만 포함한다.
- Squash Merge를 사용하므로 PR 제목도 같은 형식을 따른다.

예시:

```text
feat(A-04): 작업범위 불변 버전 생성
fix(B-02): 중복 작업 실행 차단
test(A-05): 양측 확인 충돌 검증 추가
docs: 동시 개발 규칙 정리
```

## 검증과 인계

작업 종료 전에 다음을 수행한다.

- `git status --short`와 전체 diff를 확인한다.
- `git diff --check`로 충돌 표식, trailing whitespace와 patch 오류를 확인한다.
- repository에 정의된 lint, type check, unit·integration test를 실행한다.
- migration이 있으면 head 수와 upgrade 경로를 검증한다.
- Port·event·OpenAPI 변경이 있으면 관련 문서를 갱신한다.
- 최종 보고에 작업 ID, 변경 파일, migration, 계약 변경, 실행한 테스트, 미해결 위험과 필요한 merge 순서를 포함한다.
