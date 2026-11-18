# hypothesis — 순위별 근본 원인 + 제안 수정 계획 (비구속)

범위: graph31 런의 공통 실패 `chain-01`, `chain-03`, `single-18`, `single-23`, `handoff-06`. 가설만 기술 — **이 스냅샷 단계에서는 코드를 수정하지 않음**.

---

## 순위별 가설

### H1 — 정책 수수료 `3000원`에 대한 런타임/그레이더 grounding 불일치 (최우선)

**증거:** 모든 스택에서 환불 T1 (chain-01/03)과 native single-18 T0이 `ungrounded_values=['3000원']`(근거 없는 값)과 고정 폴백 응답을 보이며, `required_call_recall=0` (또는 get_order만 있으면 0.5). 그레이더 테스트 `test_policy_value_grounded_without_tools`는 `prompts.json` common을 통해 `3000원`을 OK로 취급; 그래프 `ungrounded_values()`는 그 증거를 로드하지 않음. 체크포인트 문구도 이전 턴 툴만으로는 grounding되지 않는다고 하나 — 정책 프롬프트 값 역시 빠져 있음.

**영향:** 확인(confirmation) 턴을 중단하면 `check_refund_eligibility` / 이후 `request_refund` / `schedule_return_pickup`이 막힘 → `chain_order_correct=False`, final_state가 배송완료에 고착. Native는 회복하지 않음; Omni/Clef/Pplx는 때로 앞으로 나아가지만 여전히 refund-before-pickup을 놓침.

**적용:** chain-01, chain-03, single-18 (native 경로). single-18 Omni 경로에는 툴 성공 후 `검수` 문구 누락만 부차적으로 기여.

### H2 — 전문 에이전트가 필수 read 툴 없이 응답

**증거:** single-23은 `issue_receipt`를 절대 호출하지 않음 (대신 확인만 질문). handoff-06 T1은 `get_policy`를 절대 호출하지 않음 (할루시네이션된 정책 문장 → miss `타인`). 라우팅은 정상 (`handoff_accuracy=1.0`, payment/qna 라우트 OK).

**가능 원인:** common 프롬프트의 “항상 먼저 확인 질문”이 비-`IRREVERSIBLE` 툴에 과도 적용; qna 프롬프트는 P09를 나열하지만 메모리로 답하는 것을 금지하지 않음; ID 조회에 대한 그래프 수준 “최종 응답 전 반드시 툴” 규칙 없음.

### H3 — 연쇄 / 런타임 consent 바인딩 부재 (부차)

T1이 죽으면 이후 consent 턴에 실행할 것이 없음; 모델이 “담당 부서로 연결”을 지어내거나, 선행 `request_refund` 없이 `schedule_return_pickup`을 호출 (툴이 올바르게 오류). 후속 문서는 이미 consent 게이트를 보류 — 나중에 유용하나 첫 패치 대상은 아님.

### H4 — 올바른 툴 이후 reply-include 공백 (좁은 범위)

Omni/Clef/Pplx single-18: tools+final_state OK, 실패는 `include_missing=['검수']`뿐. 프롬프트는 이미 검수 대기를 말함; 대부분 생성 품질 / grounding 이후 누락. 그래프 변경 우선순위는 H1/H2보다 낮음.

### H5 — Transfer 캡 / escalate / step 예산

**이 다섯에 대해 배제:** transfers=0, loop_limit=false, human 라우트 없음, 실패 턴의 steps가 8 미만으로 충분.

---

## 최우선 제안 수정 (가설만)

**그래프 grounding 증거를 그레이더 정책 금액과 맞춘 뒤(H1), ID 조회에 대해 “tool-before-answer”를 강화(H2).**

### Change set A — grounding 패리티 (1차)

파일: `shop/graph.py` → `ungrounded_values`.

1. 그레이더가 정책 수수료에 대해 신뢰하는 것과 같은 출처에서 known 토큰을 추가. 최소한:
   - 포맷된 `data/prompts.json` `common` (customer_id + REFERENCE_DATE), 그리고/또는
   - `shop.db["policies"]` 텍스트 / P02 수수료, 그리고/또는
   - 제품 결정이 허용하면 **같은 shop.trace의 이전 턴** 툴이 이미 반환한 숫자 `fee` 필드 (체크포인트는 현재 이전 턴 툴을 제외 — 그레이더 테스트에 맞추려면 프롬프트/정책을 우선).
2. 진짜 할루시네이션(예: `2026-10-29`, `4321원`)은 계속 거부 — 기존 단위 테스트가 커버.
3. 선택: 폴백 완화 — ungrounded면 숫자 금액을 제거하고 막다른 고정 문장 대신 확인 질문을 유지 (불일치가 남아도 연쇄를 줄임).

**위험:** 중간 — known 집합이 너무 넓으면 실제 할루시네이션을 가릴 수 있음. 새 출처를 만들지 말고 그레이더의 증거 구성을 그대로 미러링해 완화.  
**GPU 없이 검증:** `tests/test_shop.py` 스텁 확장:
- 이 턴에 툴 **없이** 최종 답에 `3000원` / `3,000원`을 언급하는 refund 에이전트 → `grounding_failed`를 설정하지 **않아야** 하며 응답을 유지해야 함 (`test_policy_value_grounded_without_tools`와 패리티).
- 오늘과 같이 `2026-10-29`와 `4321원`은 계속 실패.
- Stub LLM으로 chain-01 T0–T2 멀티턴 스텁: 수정 후 T1은 3000원으로 확인을 묻고, T2는 `request_refund`를 실행할 수 있어야 함.

### Change set B — payment/qna tool-before-answer (2차)

파일: `data/prompts.json` (payment/qna/common), 선택적으로 `shop/agents.py` / `shop/graph.py`.

1. 프롬프트: `issue_receipt` / `get_payment` / `get_policy` / `get_reviews`는 확인 게이트 **대상이 아님**; 명확한 사용자 요청 시 실행. Irreversible 목록은 변경하지 않음.
2. qna 프롬프트: 사용자가 `P0x`를 지명하면 답하기 전에 `get_policy` 호출; 정책 문장을 지어내지 말 것.
3. 선택적 그래프: 전문 에이전트가 tool_calls 없이 최종 텍스트를 반환하고 사용자 메시지가 강한 ID 패턴(`P\d+`, `영수증`, 주문 영수증)과 맞으면, SystemMessage로 “먼저 툴을 호출하라” 한 번 재시도 강제 (grounding retry와 유사). step 예산을 염두에 둘 것.

**위험:** 프롬프트만이면 낮음–중간; 그래프 force-tool이면 중간 (지연/스텝).  
**GPU 없이 검증:** single-23과 handoff-06 T1용 Stub 시나리오; `shop.trace`의 툴 이름과 fixture 응답으로 grade_scenario 성공을 assert.

### Change set C — 이후 (1차가 아님)

`graph_v31_followup.md`에 따른 런타임 consent 게이트; `검수`용 reply-phrase 넛지.

---

## 검증 계획

### CPU / GPU 없음 (먼저)

```bash
cd /home/home/llama.cpp/bench/shopagent
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
```

라이브 재측정 전에 `tests/test_shop.py` (GraphTests / GraderTests)에 타깃 테스트를 추가. 기존 스위트는 이미 grounding/transfer/escalate를 커버하는 약 92개 테스트.

### GPU / 라이브 재측정 (이후, 별도 런 디렉터리 — 덮어쓰지 말 것)

CPU가 통과한 뒤에만. 예시 형태 (현재 `run.py` CLI / `run_naming.md`에 맞게 조정):

```bash
# 스냅샷 단계에서는 실행하지 말 것. 새 폴더명만, 예:
#   runs/g12-native-graph31-fixA-r1
# g12-native-graph31-r{1,2,3}를 재사용하지 말 것
```

CLI가 시나리오 필터를 지원하면 다섯 ID에 집중; 아니면 전체 스위트 후 해당 행 비교. Omni (`jev-g12-*` 폴더 네이밍) / Clef / Pplx graph31 fix 런도 같은 방식으로 스팟 체크.

---

## 제안 Codex / workspace-write 명령 (아직 실행하지 말 것)

WSL 프로젝트 루트에서, 상위(parent) 승인 후:

```bash
cd /home/home/llama.cpp/bench/shopagent && codex --ask-for-approval never --sandbox workspace-write exec "
Read docs/failfix-before-20261025/{hypothesis,code_map,grade_summary}.md and shop/graph.py shop/grader.py tests/test_shop.py.
Implement Change set A only: align shop.graph.ungrounded_values known-evidence with grader policy/prompt amounts (3000원) without weakening hallucination rejection.
Add/adjust unittest stubs proving policy fee is allowed without tools and 2026-10-29/4321원 still blocked.
Do not start GPU/llama-server; do not delete runs; do not overwrite runs/g12-native-graph31-r*.
Run: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
"
```

Change set A가 반영되고 CPU 테스트가 통과한 뒤 두 번째 Codex 턴으로 Change set B를 미룸.
