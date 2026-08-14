# Track B 진행상황 트래커

> 작업 **정의/수락기준**의 원본은 [`BACKEND_WORK_SPLIT.md`](BACKEND_WORK_SPLIT.md) §6. 이 문서는 그 위에 **현재 상태**만 덧씌운 트래커다.
> 기준일: 2026-08-15

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
| **B-04** | Vertex AI/Gemini adapter | ✅ | staging runtime IAM·model 실호출은 배포 검증 필요 | [#79](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/79) |
| **B-05** | 미디어 검증 + 파생 처리 | 🟡 | generation-pinned 검증 handler 완료 / 파생 format·도구 결정 필요 | — |
| **B-06** | worker 멱등성 + 오류 매핑 | ✅ | provider 재시도·복구 E2E는 INT-06 | [#81](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/81), [#82](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/82), [#84](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/84) |
| **B-07** | GCS 삭제 + 장시간 Job handler | ✅ | Cloud Tasks private worker에 삭제 handler 연결 | [#36](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/36) |

---

## Phase 3 — 공동 통합 (대부분 A 주도, B adapter 물림)

| ID | 시나리오 | 주도 | 상태 | 막는 것 |
|---|---|---|---|---|
| **INT-01** | 촬영 제출 → AI 분석 → 범위 초안 | A | ✅ | 현재 변경 병합 후 staging 분석 실호출 검증 |
| **INT-04** | 완료 미디어 → 완료 확인 → 보존 정책 | A | ✅ | 최신 main staging 실경로 검증 필요 |
| **INT-06** | task 재시도·provider 장애·복구 | B | ⬜ | provider 실패 재시도 정책과 동시 실행 recovery 검증 |

---

## 한눈에 요약

- **병합 완료:** GCS SDK #37, B-01 #39, B-02 #73, B-03 #35, B-04 #79, B-05 validation, B-06 #81·#82·#84, B-07 #36
- **INT-01 완료 범위:** MIME 계약 #85·#86, event 계약 #87, 촬영 제출·durable dispatch·범위 초안 import는 현재 변경
- **남은 구현:** INT-06과 승인된 파생 처리 정책; FE는 현재 실행 계약에 맞춘 첫 E2E slice 연동

## 의존성 흐름

```
[B-01 #39 ✅] ───────────────→ B-05
[B-01 #39 + B-02 + B-07 #36] → INT-04
[B-02 + B-03 #35 + B-04 #79 + B-06 #81/#82/#84] ─→ INT-01 ✅
[B-02 + B-04 #79 + B-06] ──────────────────────────→ INT-06 ⬜
[B-05 validation] ──────────────────────────────────→ 파생 처리 정책 결정
```
