## 작업 정보

- Task ID:
- Track: A / B / FND / INT
- Base commit:
- 관련 문서·이슈:

PR 제목은 `<type>(<Task-ID>): <간결한 한국어 요약>` 형식을 사용합니다. Task ID가 없는 공통 작업은 `<type>: <간결한 한국어 요약>`을 사용합니다.

## 변경 내용

- 해결한 문제:
- 주요 변경:
- 수정한 소유 영역:
- 수정한 공용 파일:

## 의존성과 merge 순서

- 선행 PR:
- 후속 PR:
- 권장 merge 순서:
- 다른 작업자가 rebase해야 하는 branch:

## 계약과 데이터

- [ ] API 계약 변경 없음
- [ ] Port 계약 변경 없음
- [ ] Event schema 변경 없음
- [ ] DB migration 없음

변경이 있다면 version, 호환성, consumer와 rollback 방법을 설명합니다.

## 충돌 방지 확인

- [ ] PR 제목을 간결한 한국어 커밋 메시지 형식으로 작성했습니다.
- [ ] 제목에 마침표, 이모지, AI 도구명과 모호한 표현을 넣지 않았습니다.
- [ ] 하나의 commit과 PR에 하나의 목적만 포함합니다.
- [ ] 최신 `origin/main` 기준으로 rebase했습니다.
- [ ] `git diff --check`를 통과했습니다.
- [ ] 하나의 작업 ID만 포함합니다.
- [ ] 다른 트랙의 ORM model·repository를 직접 사용하지 않습니다.
- [ ] 공용 파일 변경을 상대 담당자에게 알렸습니다.
- [ ] 동시에 열린 migration·dependency PR과 merge 순서를 확인했습니다.
- [ ] 전체 formatting·rename·관련 없는 refactor를 포함하지 않습니다.
- [ ] lockfile·생성 파일을 수동으로 합치지 않았습니다.

## 검증

- [ ] Lint / formatting
- [ ] Type check
- [ ] Unit test
- [ ] Integration test
- [ ] Migration head와 upgrade 경로
- [ ] 권한·중복 실행·재시도 실패 시나리오

실행한 명령과 결과:

```text

```

## 운영 영향

- 배포 순서:
- Feature flag 또는 비활성 경로:
- 관측 지표·경보:
- Rollback 방법:
- 남은 위험:

## AI 작업 인계

- AI가 수정한 파일:
- AI가 수정하지 않은 인접 영역:
- 사람이 확인해야 할 판단:
