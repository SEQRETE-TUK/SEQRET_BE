# Track B 진행상황 트래커

> 작업 **정의/수락기준**의 원본은 [`BACKEND_WORK_SPLIT.md`](BACKEND_WORK_SPLIT.md) §6. 이 문서는 그 위에 **현재 상태**만 덧씌운 트래커다.
> 기준일: 2026-08-16

## 범례

✅ 병합 · 🟡 코드 병합, runtime 통합 필요 · 🚫 선행·외부 의존성 대기 · ⬜ 미착수

---

## Phase 1 — 독립 기본 흐름

| ID | 작업 | 상태 | 막는 것 / 선행 | 산출물 |
|---|---|---|---|---|
| **B-01** | GCS StoragePort adapter | ✅ | production 설정·wiring은 통합 범위 | [#39](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/39) |
| **B-02** | Cloud Tasks adapter + worker runtime | ✅ | 없음 — staging enqueue → private worker 완료 검증 | [#73](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/73) |
| **B-03** | AnalysisRun + fake AI pipeline | ✅ | 없음 | [#35](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/35) |

---

## Phase 2 — 핵심 기능 확장

| ID | 작업 | 상태 | 막는 것 / 선행 | 산출물 |
|---|---|---|---|---|
| **B-04** | Vertex AI/Gemini adapter | ✅ | staging 실제 분석 completed·범위 초안 import 확인 | [#79](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/79), [#103](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/103) |
| **B-05** | 미디어 검증 + 파생 처리 정책 | ✅ | generation-pinned 검증 완료 / v1 원본-only, 파생물은 소비 요구가 생길 때 versioned 후속 계약 | — |
| **B-06** | worker 멱등성 + 오류 매핑 | ✅ | provider 재시도·복구 E2E는 INT-06 | [#81](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/81), [#82](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/82), [#84](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/84) |
| **B-07** | GCS 삭제 + 장시간 Job handler | ✅ | Cloud Tasks private worker에 삭제 handler 연결 | [#36](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/36) |

---

## Phase 3 — 공동 통합 (대부분 A 주도, B adapter 물림)

| ID | 시나리오 | 주도 | 상태 | 막는 것 |
|---|---|---|---|---|
| **INT-01** | 촬영 제출 → AI 분석 → 범위 초안 | A | ✅ | staging 실제 GCS upload·validation·Vertex completed·범위 초안 4개 import |
| **INT-04** | 완료 미디어 → 완료 확인 → 보존 정책 | A | ✅ | `f4b0619` staging GCS·Cloud Tasks·완료·문서·보존 실경로 검증 완료 |
| **INT-06** | task 재시도·provider 장애·복구 | B | 🟡 | #103·#104·staging 성공 E2E 완료 / FAILED 저장→reopen crash-window 잔여 — [#93](https://github.com/SEQRETE-TUK/SEQRET_BE/issues/93) |

---

## 한눈에 요약

- **병합 완료:** GCS SDK #37, B-01 #39, B-02 #73, B-03 #35, B-04 #79, B-05 validation, B-06 #81·#82·#84, INT-06 진단·bounded retry #103과 A PostgreSQL 검증 #104, B-07 #36
- **INT-01 완료 범위:** MIME 계약 #85·#86, event 계약 #87, 촬영 제출·durable dispatch·범위 초안 import, Vertex runtime wiring과 staging 배포 `c7b6e77`
- **INT-01 완료 증적:** [staging #31926313870](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31926313870)에서 실제 GCS upload·validation `READY`·Vertex 분석 `completed`·범위 초안 4개 import를 확인했다.
- **INT-04 완료 증적:** [staging #31881107839](https://github.com/SEQRETE-TUK/SEQRET_BE/actions/runs/31881107839)에서 실제 GCS upload·Cloud Tasks validation부터 완료 제출·고객 확인·PDF archive·30일 보존 예약까지 통과했다.
- **남은 구현:** INT-06 [#93](https://github.com/SEQRETE-TUK/SEQRET_BE/issues/93)의 FAILED 저장→reopen crash-window. FE 첫 촬영 E2E slice는 #4, A-06 분석 검토는 #5·#6으로 연동됐다. v1은 검증된 원본만 사용하며 파생물은 현재 blocker가 아니다.

## 의존성 흐름

```
[B-01 #39 ✅] ───────────────→ B-05
[B-01 #39 + B-02 + B-07 #36] → INT-04 ✅
[B-02 + B-03 #35 + B-04 #79 + B-06 #81/#82/#84] ─→ INT-01 ✅
[B-02 + B-04 #79 + B-06 + #103/#104] ─────────────→ INT-06 🟡 → #93 crash-window
[B-05 validation + v1 원본-only 정책 ✅] ───────────→ 실제 소비 요구 시 파생 계약 v2
```
