# Track B 진행상황 트래커

> 작업 **정의/수락기준**의 원본은 [`BACKEND_WORK_SPLIT.md`](BACKEND_WORK_SPLIT.md) §6. 이 문서는 그 위에 **현재 상태**만 덧씌운 트래커다.
> 기준일: 2026-08-13

## 범례
✅ 완료(로컬 검증+push) · 🟡 부분 · 🚫 착수불가(선행/의존성) · ⬜ 미착수 · 🅰️ A 주도

---

## Phase 1 — 독립 기본 흐름

| ID | 작업 | 상태 | 막는 것 / 선행 | 산출물 |
|---|---|---|---|---|
| **B-01** | GCS StoragePort adapter | ✅ | (없음) | PR #39 병합 완료 |
| **B-02** | Cloud Tasks adapter + worker runtime | 🚫 | **의존성 PR**: `google-cloud-tasks` + Cloud Tasks 큐/OIDC 인프라(A) | — |
| **B-03** | AnalysisRun + fake AI pipeline | 🟡 | migration 순서 조정·A 리뷰 | PR #35 |

**B-03 상세:** `app/modules/analysis/{models,service}.py`. 검증: ruff/mypy strict/12 tests/모듈 branch 100%/alembic 단일 head `b_03_0001`. A 리뷰 필요: `alembic/env.py`·마이그레이션·`ALEMBIC_HEAD`. ⚠️ postgres migration은 CI 검증 필요.

---

## Phase 2 — 핵심 기능 확장

| ID | 작업 | 상태 | 막는 것 / 선행 | 산출물 |
|---|---|---|---|---|
| **B-04** | Vertex AI/Gemini adapter | 🚫 | **의존성 PR**: `google-cloud-aiplatform` + `aiplatform.user` IAM(A). B-03✅ 위에서 진행 | — |
| **B-05** | 미디어 검증 + 파생 처리(썸네일) | 🚫 | B-01 선행 + Pillow/ffmpeg **의존성** | — |
| **B-06** | worker 멱등성 + 오류 매핑 | 🚫 | B-02 + B-04 선행 | — |
| **B-07** | GCS 삭제 + 장시간 Job handler | 🟡 | 핸들러 완료 / 런타임 배선은 B-02 인프라 | PR #36 |

**B-07 상세:** `app/modules/media_processing/deletion.py`. 검증: ruff/mypy strict/3 tests/핸들러 branch 100%. 공용파일·마이그레이션·새 의존성 0. **남은 것:** Cloud Run Job 런타임(=B-02)에서 이 핸들러를 실제 호출하는 배선.

---

## Phase 3 — 공동 통합 (대부분 A 주도, B adapter 물림)

| ID | 시나리오 | 주도 | 상태 | 막는 것 |
|---|---|---|---|---|
| **INT-01** | 촬영 제출 → AI 분석 → 범위 초안 | B | 🚫 | B-04 + A `ImportAnalysisDraft`(A쪽 존재). B-03✅ |
| **INT-02** | 양측 확인 → 범위 잠금 | 🅰️ A | 🅰️ | A 트랙 |
| **INT-03** | 변경 증거 → 변경요청 → 새 버전 | 🅰️ A | 🅰️ | A 트랙 |
| **INT-04** | 완료 미디어 → 완료 확인 → 보존 정책 | 🅰️ A | 🅰️ | A 트랙 (B-07 핸들러 연동) |
| **INT-05** | 토큰 만료·철회·권한 공격 | 🅰️ A | 🅰️ | A 트랙 |
| **INT-06** | task 재시도·provider 장애·복구 | B | 🚫 | B-02 + B-04 + B-06 선행 |
| **INT-07** | 배포·migration·복구 | 🅰️ A | 🅰️ | A 트랙 |

---

## 한눈에 요약

- **병합 완료:** B-01 PR #39
- **리뷰 중:** B-03 PR #35, B-07 PR #36
- **의존성:** GCS SDK PR #37은 병합 완료. Cloud Tasks·Vertex AI SDK와 A 인프라는 아직 필요
- **다음 순서:** B-01 → B-07 런타임 배선, B-02·B-04 → B-06 → INT-01/INT-06

## 의존성 흐름

```
[GCS SDK #37 + B-01 #39 ✅] ─┬─→ B-05
                            └─→ B-07 런타임 배선
[Cloud Tasks SDK + A 인프라] ─→ B-02 ─┬─→ B-06 ─┐
[Vertex AI SDK] ───────────────→ B-04 ─┴─────────┼─→ INT-06
                                      B-04 ──────┴─→ INT-01
B-03 #35 ──────────────────────────────────────────→ INT-01
```
