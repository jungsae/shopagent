# failfix-after-20261025-A — Change set A (grounding 패리티)

날짜: 2026-10-25 (Asia/Seoul). 범위: Change set A만. Change set B (issue_receipt / get_policy 프롬프트)는 미구현.

## 변경 내용

### shop/graph.py — ungrounded_values
- ROOT, REFERENCE_DATE, load를 shop.shared에서 import.
- HumanMessage + 동일 턴 툴 증거 뒤에, 포맷된 data/prompts.json common의 value_tokens도 union (customer_id=shop.customer_id, reference_date=REFERENCE_DATE).
- shop/grader.py(~228행)의 그레이더 증거와 같은 취지로, common 프롬프트(P02형 단순변심 반품비)의 정책 수수료 3000원 / 3,000원을 동일 턴 툴 없이도 허용.
- 여전히 known 밖이면서 숫자로 시작하는 토큰만 ungrounded로 취급; 지어낸 날짜/금액(예: 2026-10-29, 4321원)은 계속 차단.
- 폴백 완화 없음; 이전 턴 툴 grounding 없음; Change B 프롬프트/그래프 tool-before-answer 작업 없음.

### tests/test_shop.py — GraphTests
- test_policy_value_grounded_without_tools 추가: 툴 없이 3000원 / 3,000원이 들어간 refund_exchange 최종 응답 → grounding_retries==0, grounding_failed==False, 응답 유지.
- test_unseen_values_trigger_one_grounding_retry를 4321원 포함으로 확장.
- test_grounding_retry_cannot_ground_itself_or_repeat_mutations에 4321원 두 번째 응답 케이스를 추가하고 visible에 남지 않음을 assert.

## 이유
라이브 graph31 공통 실패에서 chain-01/03 T1과 native single-18 T0이 ungrounded_values=["3000원"]으로 중단되며 고정 grounding 폴백이 나와, 필수 환불 툴이 실행되지 않음. 그레이더는 이미 common 프롬프트를 통해 해당 수수료를 known으로 취급; 런타임은 그렇지 않았음.

## 실행한 테스트
명령:
cd /home/home/llama.cpp/bench/shopagent && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q

결과: Ran 93 tests in ~5s — OK

## 변경된 파일
- shop/graph.py
- tests/test_shop.py

## 미완료 (Change set B — 남은 실패)
- single-23: payment이 issue_receipt를 호출하지 않음 (대신 확인만 질문).
- handoff-06 T1: qna가 get_policy(P09)를 호출하지 않음 (지어낸 문장; miss 타인).
- Omni/Clef/Pplx single-18 reply-include 검수 공백 (생성 품질; 우선순위 낮음).
