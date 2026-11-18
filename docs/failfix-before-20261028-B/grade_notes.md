# grade_notes — g12-native-graph31-fixA-r1 잔여 8실패

추출일: 2026-10-28 (Asia/Seoul). 소스: `runs/g12-native-graph31-fixA-r1/results.json`.

## single-12
- success=false. T0: required get_order recall=0, confirmation_correct=false (확인만, 툴 0). T1: cancel_order recall=0 (get_order만).
- final_state: orders.O001.status 결제완료≠취소, refunded/refund_status 미반영.
- 범위: IRREVERSIBLE consent 경로 → Change B 밖.

## single-23 (H2 / B)
- success=false. payment 라우트 OK. tool_count=0 (transfer만). 확인 질문만.
- include_missing=['R-O002']; final_state payments.O002.receipt_issued expected true actual false.
- ask_confirmation=false 시나리오인데 모델이 확인 게이트를 적용.

## single-29
- get_policy(P05) 호출 성공, final_state OK. include_missing=['출고'] (reply 문구만).
- B의 “지어내지 말 것”과 간접 관련, 주실패는 reply-include.

## handoff-03
- T0 get_refund_status OK. T1 get_order만, track_shipment miss → include_missing=['TRK000000003'].
- B 대상 툴 아님.

## handoff-06 (H2 / B)
- T0 get_reviews OK. T1 qna로 전환 후 get_policy 없이 정책 할루시네이션 → include_missing=['타인'], recall=0.

## handoff-07
- T0 list_payment_methods 미호출, 결제수단 날조 → include_missing=['간편결제']. T1 check_stock OK.
- B 명시 목록 외 (list_payment_methods).

## handoff-08 (H2 유사 / B 기대)
- T0 get_order OK. T1 get_policy(P05) 없이 정책 날조 → include_missing=['출고'].

## policy-01
- refund_exchange 라우트, 툴 0, 상담원 연결 제안. required 2콜 miss, include_missing=['불가'].
- B 밖.
