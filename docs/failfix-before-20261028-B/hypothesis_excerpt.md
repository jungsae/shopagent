# hypothesis 발췌 — H2 / Change set B

출처: `docs/failfix-before-20261025/hypothesis.md` (원문 유지, 여기선 발췌만).

## H2 — 전문 에이전트가 필수 read 툴 없이 응답

증거: single-23은 `issue_receipt`를 절대 호출하지 않음 (대신 확인만 질문). handoff-06 T1은 `get_policy`를 절대 호출하지 않음 (할루시네이션된 정책 문장 → miss `타인`). 라우팅은 정상.

가능 원인: common 프롬프트의 “항상 먼저 확인 질문”이 비-IRREVERSIBLE 툴에 과도 적용; qna 프롬프트는 P09를 나열하지만 메모리로 답하는 것을 금지하지 않음; ID 조회에 대한 그래프 수준 “최종 응답 전 반드시 툴” 규칙 없음.

## Change set B — payment/qna tool-before-answer

파일: `data/prompts.json` (payment/qna/common), 선택적으로 `shop/agents.py` / `shop/graph.py`.

1. 프롬프트: `issue_receipt` / `get_payment` / `get_policy` / `get_reviews`는 확인 게이트 대상이 아님; 명확한 사용자 요청 시 실행. Irreversible 목록은 변경하지 않음.
2. qna 프롬프트: 사용자가 `P0x`를 지명하면 답하기 전에 `get_policy` 호출; 정책 문장을 지어내지 말 것.
3. 선택적 그래프: 전문 에이전트가 tool_calls 없이 최종 텍스트를 반환하고 사용자 메시지가 강한 ID 패턴(`P\d+`, `영수증`, 주문 영수증)과 맞으면, SystemMessage로 “먼저 툴을 호출하라” 한 번 재시도 강제. step 예산을 염두에 둘 것.

위험: 프롬프트만이면 낮음–중간; 그래프 force-tool이면 중간 (지연/스텝).
GPU 없이 검증: single-23과 handoff-06 T1용 Stub 시나리오.
