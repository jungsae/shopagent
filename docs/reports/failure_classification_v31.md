# ShopAgent 실패 분류 v3.1 (pre-GPU)

- 작성 목적: rescore 이후 NEW-fail 원인을 버킷 A–H로 분류해 그래프 구조 수정 우선순위를 정함.
- 데이터: `runs/*/results_rescored_v2.json` (7개) + `data/scenarios.json` 기대치.
- 규칙: JSON 증거(routes/steps/tool_count/transfers/loop_limit/calls/grade/RFT)만 사용. 추정 시 명시.
- GPU/remeasure 없음.

## 버킷 정의

| 버킷 | 의미 |
|---|---|
| A | escalate_to_human 누락 / human 라우트 미실행 |
| B | handoff/transfer_back_to_supervisor 캡(현재 1회) 가능 |
| C | step>=8 또는 tools>=6 리밋 적중 |
| D | missing required reply / reply_must_include |
| E | final_state mismatch |
| F | hallucinated_value |
| G | missing refusal phrase |
| H | other |

Primary = 우선순위 A>B>C>D>E>F>G>H 중 해당 시나리오에 붙은 첫 버킷. Multi = 한 시나리오에 여러 버킷이 동시에 붙을 수 있음.

## 전체 런 요약 (primary counts)

| run | pass | fail | A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|---|---|---|
| g12-native-r1 | 46 | 14 | 3 | 0 | 0 | 7 | 1 | 3 | 0 | 0 |
| g12-native-r2 | 46 | 14 | 3 | 0 | 0 | 7 | 1 | 3 | 0 | 0 |
| g12-native-r3 | 46 | 14 | 3 | 0 | 0 | 7 | 1 | 3 | 0 | 0 |
| g12-native-ngl0-r1 | 50 | 10 | 3 | 0 | 0 | 6 | 0 | 1 | 0 | 0 |
| clef-g12-r1 | 48 | 12 | 1 | 0 | 0 | 5 | 2 | 4 | 0 | 0 |
| jev-g12-r1 | 49 | 11 | 1 | 0 | 0 | 7 | 1 | 2 | 0 | 0 |
| pplx-g12-r1 | 48 | 12 | 1 | 0 | 0 | 7 | 2 | 2 | 0 | 0 |

## 전체 런 요약 (multi-label counts)

| run | fail | A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|---|---|
| g12-native-r1 | 14 | 3 | 0 | 0 | 7 | 5 | 3 | 1 | 0 |
| g12-native-r2 | 14 | 3 | 0 | 0 | 7 | 5 | 3 | 1 | 0 |
| g12-native-r3 | 14 | 3 | 0 | 0 | 7 | 5 | 3 | 1 | 0 |
| g12-native-ngl0-r1 | 10 | 3 | 0 | 0 | 6 | 5 | 3 | 0 | 0 |
| clef-g12-r1 | 12 | 1 | 0 | 0 | 5 | 4 | 4 | 0 | 0 |
| jev-g12-r1 | 11 | 1 | 0 | 0 | 7 | 3 | 2 | 0 | 0 |
| pplx-g12-r1 | 12 | 1 | 0 | 0 | 7 | 4 | 2 | 0 | 0 |

## g12-native-r1 상세 (NEW-fail = success=False, n=14)

- pass=46, fail=14
- primary: {"E": 1, "D": 7, "F": 3, "A": 3}
- multi: {"E": 5, "D": 7, "F": 3, "A": 3, "G": 1}

| id | primary | labels | routes | steps | tools | transfers | loop_limit | escalate? | RFT | 증거 메모 |
|---|---|---|---|---:|---:|---:|---|---|---|---|
| single-12 | E | E | order_delivery,order_delivery | 4 | 1 | 0 | False | False | final_state mismatch; required call missing / wrong arguments | final_state_errors=[{"path": "orders.O001.status", "expected": "취소", "actual": "결제완료"}, {"path": "payments.O001.refunded", "expected": 30000, "actual": 0}]; also: required call mis |
| single-23 | D | D,E | payment | 2 | 0 | 0 | False | False | final_state mismatch; missing required reply; required call missing / wrong arguments | include_missing=['R-O002'] empty_reply_turns=0; final_state_errors=[{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}]; also: required call missing/wrong  |
| single-29 | D | D | qna | 4 | 1 | 0 | False | False | missing required reply | include_missing=['출고'] empty_reply_turns=0 |
| chain-01 | F | F | order_delivery,refund_exchange,refund_exchange,refund_exchange,refund_exchange | 6 | 2 | 0 | False | False | hallucinated_value; required call missing / wrong arguments | hallucinated_values=['2026-10-29'] viol=1 |
| chain-03 | F | F | order_delivery,refund_exchange,refund_exchange,refund_exchange,refund_exchange | 6 | 2 | 0 | False | False | hallucinated_value; required call missing / wrong arguments | hallucinated_values=['2026-10-29'] viol=1 |
| chain-06 | F | F | order_delivery,refund_exchange,refund_exchange,refund_exchange,refund_exchange | 6 | 2 | 0 | False | False | hallucinated_value; required call missing / wrong arguments | hallucinated_values=['2026-10-29'] viol=1 |
| handoff-03 | D | D | payment,order_delivery | 4 | 1 | 0 | False | False | missing required reply; required call missing / wrong arguments | include_missing=['TRK000000003'] empty_reply_turns=1; also: required call missing/wrong args |
| handoff-05 | A | A,E | refund_exchange,human | 6 | 2 | 0 | False | False | final_state mismatch; handoff tool not called | human_routed=True need_esc=True has_escalate=False rft_handoff=True; final_state_errors=[{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": tru |
| handoff-06 | D | D | product,qna | 4 | 1 | 0 | False | False | missing required reply; required call missing / wrong arguments | include_missing=['타인'] empty_reply_turns=0; also: required call missing/wrong args |
| handoff-07 | D | D | payment,product | 4 | 1 | 0 | False | False | missing required reply; required call missing / wrong arguments | include_missing=['간편결제'] empty_reply_turns=0; also: required call missing/wrong args |
| handoff-08 | D | D | order_delivery,qna | 4 | 1 | 0 | False | False | missing required reply; required call missing / wrong arguments | include_missing=['출고'] empty_reply_turns=0; also: required call missing/wrong args |
| policy-01 | D | D,G | refund_exchange | 2 | 0 | 0 | False | False | missing refusal phrase; required call missing / wrong arguments | include_missing=['불가'] empty_reply_turns=0; rft missing refusal phrase |
| policy-06 | A | A,E | human | 2 | 0 | 0 | False | False | final_state mismatch; handoff tool not called | human_routed=True need_esc=True has_escalate=False rft_handoff=True; final_state_errors=[{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": tru |
| policy-07 | A | A,E | human | 2 | 0 | 0 | False | False | final_state mismatch; handoff tool not called | human_routed=True need_esc=True has_escalate=False rft_handoff=True; final_state_errors=[{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": tru |

### g12-native-r1 시나리오별 근거

#### single-12 → primary **E** (labels=['E'])
- category=single, old_success=False
- routes=['order_delivery', 'order_delivery'], steps=4, tool_count=1, transfers=0, loop_limit=False
- tools_called=['get_order']
- expected_required_names=['get_order', 'cancel_order']
- has_escalate_to_human=False
- RFT=['final_state mismatch', 'required call missing / wrong arguments']
- notes=['final_state_errors=[{"path": "orders.O001.status", "expected": "취소", "actual": "결제완료"}, {"path": "payments.O001.refunded", "expected": 30000, "actual": 0}]', 'also: required call missing/wrong args']

#### single-23 → primary **D** (labels=['D', 'E'])
- category=single, old_success=False
- routes=['payment'], steps=2, tool_count=0, transfers=0, loop_limit=False
- tools_called=[]
- expected_required_names=['issue_receipt']
- has_escalate_to_human=False
- include_missing=['R-O002']
- RFT=['final_state mismatch', 'missing required reply', 'required call missing / wrong arguments']
- notes=["include_missing=['R-O002'] empty_reply_turns=0", 'final_state_errors=[{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}]', 'also: required call missing/wrong args']

#### single-29 → primary **D** (labels=['D'])
- category=single, old_success=False
- routes=['qna'], steps=4, tool_count=1, transfers=0, loop_limit=False
- tools_called=['get_policy']
- expected_required_names=['get_policy']
- has_escalate_to_human=False
- include_missing=['출고']
- RFT=['missing required reply']
- notes=["include_missing=['출고'] empty_reply_turns=0"]

#### chain-01 → primary **F** (labels=['F'])
- category=chain, old_success=True
- routes=['order_delivery', 'refund_exchange', 'refund_exchange', 'refund_exchange', 'refund_exchange'], steps=6, tool_count=2, transfers=0, loop_limit=False
- tools_called=['get_order', 'check_refund_eligibility', 'request_refund', 'schedule_return_pickup']
- expected_required_names=['get_order', 'get_order', 'check_refund_eligibility', 'request_refund', 'get_order', 'schedule_return_pickup']
- has_escalate_to_human=False
- hallucinated_values=['2026-10-29']
- RFT=['hallucinated_value', 'required call missing / wrong arguments']
- notes=["hallucinated_values=['2026-10-29'] viol=1"]

#### chain-03 → primary **F** (labels=['F'])
- category=chain, old_success=False
- routes=['order_delivery', 'refund_exchange', 'refund_exchange', 'refund_exchange', 'refund_exchange'], steps=6, tool_count=2, transfers=0, loop_limit=False
- tools_called=['get_order', 'check_refund_eligibility', 'request_refund', 'schedule_return_pickup']
- expected_required_names=['get_order', 'get_order', 'check_refund_eligibility', 'request_refund', 'get_order', 'schedule_return_pickup']
- has_escalate_to_human=False
- hallucinated_values=['2026-10-29']
- RFT=['hallucinated_value', 'required call missing / wrong arguments']
- notes=["hallucinated_values=['2026-10-29'] viol=1"]

#### chain-06 → primary **F** (labels=['F'])
- category=chain, old_success=False
- routes=['order_delivery', 'refund_exchange', 'refund_exchange', 'refund_exchange', 'refund_exchange'], steps=6, tool_count=2, transfers=0, loop_limit=False
- tools_called=['get_order', 'check_refund_eligibility', 'request_refund', 'schedule_return_pickup']
- expected_required_names=['get_order', 'get_order', 'check_refund_eligibility', 'request_refund', 'get_order', 'schedule_return_pickup']
- has_escalate_to_human=False
- hallucinated_values=['2026-10-29']
- RFT=['hallucinated_value', 'required call missing / wrong arguments']
- notes=["hallucinated_values=['2026-10-29'] viol=1"]

#### handoff-03 → primary **D** (labels=['D'])
- category=handoff, old_success=False
- routes=['payment', 'order_delivery'], steps=4, tool_count=1, transfers=0, loop_limit=False
- tools_called=['get_refund_status', 'get_order']
- expected_required_names=['get_refund_status', 'get_order', 'track_shipment']
- has_escalate_to_human=False
- include_missing=['TRK000000003']
- RFT=['missing required reply', 'required call missing / wrong arguments']
- notes=["include_missing=['TRK000000003'] empty_reply_turns=1", 'also: required call missing/wrong args']

#### handoff-05 → primary **A** (labels=['A', 'E'])
- category=handoff, old_success=False
- routes=['refund_exchange', 'human'], steps=6, tool_count=2, transfers=0, loop_limit=False
- tools_called=['get_order', 'check_refund_eligibility']
- expected_required_names=['check_refund_eligibility', 'escalate_to_human']
- has_escalate_to_human=False
- RFT=['final_state mismatch', 'handoff tool not called']
- notes=['human_routed=True need_esc=True has_escalate=False rft_handoff=True', 'final_state_errors=[{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}]']

#### handoff-06 → primary **D** (labels=['D'])
- category=handoff, old_success=False
- routes=['product', 'qna'], steps=4, tool_count=1, transfers=0, loop_limit=False
- tools_called=['get_reviews']
- expected_required_names=['get_reviews', 'get_policy']
- has_escalate_to_human=False
- include_missing=['타인']
- RFT=['missing required reply', 'required call missing / wrong arguments']
- notes=["include_missing=['타인'] empty_reply_turns=0", 'also: required call missing/wrong args']

#### handoff-07 → primary **D** (labels=['D'])
- category=handoff, old_success=False
- routes=['payment', 'product'], steps=4, tool_count=1, transfers=0, loop_limit=False
- tools_called=['check_stock']
- expected_required_names=['list_payment_methods', 'check_stock']
- has_escalate_to_human=False
- include_missing=['간편결제']
- RFT=['missing required reply', 'required call missing / wrong arguments']
- notes=["include_missing=['간편결제'] empty_reply_turns=0", 'also: required call missing/wrong args']

#### handoff-08 → primary **D** (labels=['D'])
- category=handoff, old_success=False
- routes=['order_delivery', 'qna'], steps=4, tool_count=1, transfers=0, loop_limit=False
- tools_called=['get_order']
- expected_required_names=['get_order', 'get_policy']
- has_escalate_to_human=False
- include_missing=['출고']
- RFT=['missing required reply', 'required call missing / wrong arguments']
- notes=["include_missing=['출고'] empty_reply_turns=0", 'also: required call missing/wrong args']

#### policy-01 → primary **D** (labels=['D', 'G'])
- category=policy, old_success=False
- routes=['refund_exchange'], steps=2, tool_count=0, transfers=0, loop_limit=False
- tools_called=[]
- expected_required_names=['get_order', 'check_refund_eligibility']
- has_escalate_to_human=False
- include_missing=['불가']
- RFT=['missing refusal phrase', 'required call missing / wrong arguments']
- notes=["include_missing=['불가'] empty_reply_turns=0", 'rft missing refusal phrase']

#### policy-06 → primary **A** (labels=['A', 'E'])
- category=policy, old_success=False
- routes=['human'], steps=2, tool_count=0, transfers=0, loop_limit=False
- tools_called=[]
- expected_required_names=['escalate_to_human']
- has_escalate_to_human=False
- RFT=['final_state mismatch', 'handoff tool not called']
- notes=['human_routed=True need_esc=True has_escalate=False rft_handoff=True', 'final_state_errors=[{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}]']

#### policy-07 → primary **A** (labels=['A', 'E'])
- category=policy, old_success=False
- routes=['human'], steps=2, tool_count=0, transfers=0, loop_limit=False
- tools_called=[]
- expected_required_names=['escalate_to_human']
- has_escalate_to_human=False
- RFT=['final_state mismatch', 'handoff tool not called']
- notes=['human_routed=True need_esc=True has_escalate=False rft_handoff=True', 'final_state_errors=[{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}]']

## 분류에서 본 그래프 수정 함의

1. **Deterministic human path (A)**: g12-native-r1에서 handoff-05/policy-06/policy-07이 route=`human`인데 `escalate_to_human` 미호출(프로즈만). 전 런 primary A 합≈15. → 그래프가 human 라우트 시 도구를 Gemma에 맡기지 말고 확정 실행해야 함.
2. **transfer_back cap 1→3 (B)**: g12-native-r1 NEW-fail에서는 transfers=0이라 캡 적중 증거 없음(primary B=0). 다만 구조 제약으로 멀티 핸드오프 턴을 막을 수 있어 **유지 권고**(useless로 단정할 증거 없음). 전 런 primary B 합≈0.
3. **step/tool limit (C)**: g12-native-r1 fail에서 loop_limit/steps>=8/tools>=6 적중 없음(primary C=0). 전 런 primary C 합≈0.
4. **Post-answer grounding (F)**: chain-01/03/06 등에서 `2026-10-29` 환각(도구 결과에 없는 마감일). 전 런 primary F 합≈18. → 최종 답변의 날짜/금액이 tool/user에 없으면 재시도 또는 마크.
5. **D/G**: 주로 모델 응답 품질(필수 문구/거절 문구). 그래프만으로 완전 해결은 어려우나 grounding/human path와 병행.
6. **Irreversible gate (optional)**: single-12는 확인 질문 후 `cancel_order` 미실행 → final_state 실패. 확인 게이트는 follow-up으로 문서화 가능.

## 기타 런 fail id 목록 (요약)

- **g12-native-r2** fail=14: single-12(E), single-23(D), single-29(D), chain-01(F), chain-03(F), chain-06(F), handoff-03(D), handoff-05(A), handoff-06(D), handoff-07(D), handoff-08(D), policy-01(D), policy-06(A), policy-07(A)
- **g12-native-r3** fail=14: single-12(E), single-23(D), single-29(D), chain-01(F), chain-03(F), chain-06(F), handoff-03(D), handoff-05(A), handoff-06(D), handoff-07(D), handoff-08(D), policy-01(D), policy-06(A), policy-07(A)
- **g12-native-ngl0-r1** fail=10: single-18(D), single-23(D), single-29(D), chain-01(D), chain-03(F), handoff-05(A), handoff-06(D), handoff-08(D), policy-06(A), policy-07(A)
- **clef-g12-r1** fail=12: single-12(E), single-18(D), single-20(E), single-23(D), single-27(D), chain-01(F), chain-03(F), chain-04(F), chain-06(F), handoff-06(D), policy-07(A), robust-03(D)
- **jev-g12-r1** fail=11: single-07(D), single-18(D), single-20(E), single-23(D), single-29(D), chain-01(F), chain-04(F), handoff-06(D), handoff-08(D), policy-07(A), robust-03(D)
- **pplx-g12-r1** fail=12: single-07(D), single-12(E), single-18(D), single-20(E), single-23(D), single-29(D), chain-01(F), chain-04(F), handoff-06(D), handoff-08(D), policy-07(A), robust-03(D)

---
생성 스크립트: build_classification_v31.py (CPU only)
