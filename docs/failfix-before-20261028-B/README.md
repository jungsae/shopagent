# failfix-before-20261028-B — Change set B 적용 전 스냅샷

날짜: 2026-10-28 (Asia/Seoul).  
기준 런: `runs/g12-native-graph31-fixA-r1` (Change A 반영 후 잔여 실패).  
범위: Change set B만 (프롬프트 tool-before-answer + 선택적 그래프 force-tool). GPU 재벤치/서버 기동 없음.

선행 스냅샷 `docs/failfix-before-20261025/` 및 `failfix-after-20261025-A/`는 **삭제하지 않음**.

## 잔여 8실패 분류 (fixA-r1)

| ID | 실패 요약 | Change B 범위? |
|---|---|---|
| single-23 | `issue_receipt` 미호출(확인만 질문). `include_missing=['R-O002']`, `final_state receipt_issued=false` | **예 (H2)** |
| handoff-06 T1 | `get_policy(P09)` 미호출, 정책 문장 할루시네이션 → `include_missing=['타인']`, recall=0 | **예 (H2)** |
| single-12 | 취소 확인 게이트/미실행. T0 `get_order` miss + conf_ok=false; T1 `cancel_order` 미실행 → final_state 미반영 | 아니오 (IRREVERSIBLE/consent; Change C 쪽) |
| single-29 | `get_policy(P05)` 호출·final_state OK이나 reply에 `출고` 누락 (`include_missing`) | 부분—도구 호출은 이미 성공. B의 qna 강화가 문구에 간접 도움 가능하나 주원인은 reply-phrase(H4류) |
| handoff-03 T1 | `get_order`만 하고 `track_shipment` 누락 → `include_missing=['TRK000000003']` | 아니오 (order_delivery 연쇄; B 대상 툴 아님) |
| handoff-07 T0 | `list_payment_methods` 미호출, 결제수단 문장 날조 → `include_missing=['간편결제']` | 부분—`list_payment_methods`는 B 명시 목록에 없으나 payment “확인 비대상 read” 취지와 인접. 본 변경은 issue_receipt/get_payment/get_policy/get_reviews에 한정 |
| handoff-08 T1 | `get_policy(P05)` 미호출·정책 날조 → `include_missing=['출고']` | **예에 가깝음** (handoff-06과 동일 H2 패턴; B qna 규칙으로 커버 기대) |
| policy-01 | refund_exchange로 라우트 후 툴 0회, escalate 제안. required 2콜 miss, `include_missing=['불가']` | 아니오 (라우트/escalate/정책 예외 경로; B 프롬프트 범위 밖) |

가설 원문 발췌: `docs/failfix-before-20261025/hypothesis.md` Change set B / H2.

## 스냅샷 파일

- `code/data__prompts.json`, `code/shop__agents.py`, `code/shop__graph.py`, `code/tests__test_shop.py` — 편집 직전 바이트 복사
- `SHA256SUMS` — 위 파일 해시
- `hypothesis_excerpt.md` — H2 + Change set B 발췌
- `grade_notes.md` — 위 8실패 그레이드 노트
