# Track B 진행상황 트래커

> 작업 **정의/수락기준**의 원본은 [`BACKEND_WORK_SPLIT.md`](BACKEND_WORK_SPLIT.md) §6. 이 문서는 그 위에 **현재 상태**만 덧씌운 트래커다.
> 기준일: 2026-08-13

## 범례

✅ 병합 · 🟡 코드 병합, runtime 통합 필요 · 🚫 선행·외부 의존성 대기 · ⬜ 미착수

---

## Phase 1 — 독립 기본 흐름

| ID | 작업 | 상태 | 막는 것 / 선행 | 산출물 |
|---|---|---|---|---|
| **B-01** | GCS StoragePort adapter | ✅ | production 설정·wiring은 통합 범위 | [#39](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/39) |
| **B-02** | Cloud Tasks adapter + worker runtime | 🚫 | Cloud Tasks SDK, queue, OIDC identity와 private entrypoint | — |
| **B-03** | AnalysisRun + fake AI pipeline | ✅ | B-04와 INT-01 후속 | [#35](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/35) |

---

## Phase 2 — 핵심 기능 확장

| ID | 작업 | 상태 | 막는 것 / 선행 | 산출물 |
|---|---|---|---|---|
| **B-04** | Vertex AI/Gemini adapter | 🚫 | Vertex AI SDK, runtime IAM과 model 설정 | — |
| **B-05** | 미디어 검증 + 파생 처리 | 🚫 | A validation command 병합 / handler·지원 format·도구 결정 필요 | — |
| **B-06** | worker 멱등성 + 오류 매핑 | 🚫 | B-02 + B-04 | — |
| **B-07** | GCS 삭제 + 장시간 Job handler | 🟡 | handler 병합 / B-02·Job runtime 배선 필요 | [#36](https://github.com/SEQRETE-TUK/SEQRET_BE/pull/36) |

---

## Phase 3 — 공동 통합 (대부분 A 주도, B adapter 물림)

| ID | 시나리오 | 주도 | 상태 | 막는 것 |
|---|---|---|---|---|
| **INT-01** | 촬영 제출 → AI 분석 → 범위 초안 | B | 🚫 | B-02 + B-04, 분석 runner와 B-03 결과 연결 |
| **INT-04** | 완료 미디어 → 완료 확인 → 보존 정책 | A | 🟡 | B-07 handler를 실제 Job runtime에 연결 |
| **INT-06** | task 재시도·provider 장애·복구 | B | 🚫 | B-02 + B-04 + B-06 |

---

## 한눈에 요약

- **병합 완료:** GCS SDK #37, B-01 #39, B-03 #35, B-07 handler #36
- **남은 구현:** B-02, B-04, B-05, B-06과 provider/runtime wiring
- **다음 순서:** B-02·B-04 → B-06, 병렬로 B-05 → INT-01·INT-04·INT-06

## 의존성 흐름

```
[B-01 #39 ✅] ───────────────→ B-05
[B-01 #39 + B-02 + B-07 #36] → 삭제 Job runtime → INT-04
[Cloud Tasks 외부 준비] ─────→ B-02 ─┬─→ B-06 ─→ INT-06
[Vertex AI 외부 준비] ────────→ B-04 ─┘
[B-02 + B-03 #35 + B-04] ────→ INT-01
```
