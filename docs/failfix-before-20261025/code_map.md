# code_map — 5개 공통 실패가 건드리는 동작

경로는 `/home/home/llama.cpp/bench/shopagent` 기준 상대 경로. 줄 번호는 이 폴더의 SHA256SUMS 스냅샷(`code/`) 기준.

---

## 1. Supervisor 라우팅

| location | role |
|---|---|
| `shop/shared.py:16` | `AGENTS = ("product", "order_delivery", "refund_exchange", "payment", "qna", "human")` |
| `shop/agents.py:12-13` | `handoff(name)` → 인자 없는 transfer 툴 스키마 |
| `shop/agents.py:79-87` | `Agents.supervise` native: `tool_choice="required"`, 정확히 하나의 `transfer_to_<agent>` 호출 필수 |
| `shop/agents.py:88-102` | decision-backend 경로: `backend.decide` → argmax 라우트 |
| `shop/graph.py:44-53` | `supervisor` 노드: 라우트 append, human이면 `escalate` 아니면 `agent_llm`로 goto |
| `shop/graph.py:137-141` | 그래프 배선: START → supervisor → {agent_llm, escalate, END} |

인용 (`graph.py` supervisor goto):
```python
return Command(..., goto="escalate" if route == "human" else "agent_llm")
```

실행됨: 다섯 전부 (초기 라우트). handoff-06 T1은 `transfer_back`이 아니라 새 사용자 턴으로 supervisor에 재진입; handoff_accuracy=1.0이므로 라우팅 자체는 문제 없음.

---

## 2. Handoff / transfer_back / MAX_TRANSFER_BACK

| location | role |
|---|---|
| `shop/graph.py:14` | `MAX_TRANSFER_BACK_PER_TURN = 3` |
| `shop/graph.py:101-109` | tools 노드: alone/무인자/캡 검증; 성공 시 `target = "supervisor"` |
| `shop/agents.py:104-105` | 전문 에이전트 툴 = `schemas(agent) + [handoff("transfer_back_to_supervisor")]` |
| `shop/grader.py:241-242, 262-272` | `handoff_correct` / `handoff_accuracy` / 시나리오 `call_order` |

인용:
```python
MAX_TRANSFER_BACK_PER_TURN = 3
...
if name == "transfer_back_to_supervisor":
    if transfers >= MAX_TRANSFER_BACK_PER_TURN or len(calls) != 1 or call["args"]:
        ...
```

가볍게만 실행됨: 이 다섯 실패는 **transfers=0**; handoff-06은 `transfer_back` 없이 턴 간 라우트 변경에 성공. 캡이 실패 모드는 아님 (pre-v31 버킷 B와 대비).

---

## 3. Escalate / human

| location | role |
|---|---|
| `shop/graph.py:55-65` | 결정적 `escalate` 노드 → `shop.invoke("escalate_to_human", ...)` |
| `shop/tools.py:17, 154-159` | 스코프 + 구현 |

이 다섯의 실패 모드가 아님 (human 라우트 없음). 기존 단위 테스트 `test_human_route_escalates_*`가 커버.

---

## 4. Ungrounded values / grounding retry (chain-01/03/single-18의 PRIMARY)

| location | role |
|---|---|
| `shop/graph.py:34-40` | `ungrounded_values(reply, messages, shop, turn)` — **HumanMessage + 현재 턴 툴 출력만** |
| `shop/graph.py:67-84` | `agent_llm`: 최종 텍스트에 숫자로 시작하는 ungrounded 토큰이 있으면 → SystemMessage로 한 번 재작성; 아니면 고정 폴백, `grounding_failed` |
| `shop/grader.py:228-236` | 그레이더 known 집합 = 사용자 턴 + 툴 출력 + **`prompts.json` common** (+ args 날짜) |

런타임 인용:
```python
def ungrounded_values(reply, messages, shop, turn):
    known = value_tokens("\n".join(m.content for m in messages if isinstance(m, HumanMessage)))
    for call in shop.trace:
        if call.get("kind") == "tool" and call["turn"] == turn:
            known |= output_values(call["output"])
    return sorted(value for value in value_tokens(reply) - known if value[0].isdigit())
```

그레이더 인용 (툴 없이도 정책 수수료 허용):
```python
evidence += "\n" + load(ROOT / "data/prompts.json")["common"].format(...)
known = value_tokens(evidence, reference_date, base_year)
```

`data/prompts.json` common 텍스트에 `단순변심 반품비 3000원` 포함. 단위 테스트 `test_policy_value_grounded_without_tools` / `test_policy_grounding_uses_prompt_values`가 그레이더 동작을 인코딩 — **그래프 런타임은 그렇지 않음**.

관측: T1 ungrounded=`['3000원']` → 고정 응답 `확인된 정보만으로 날짜나 금액을 안내하기 어렵습니다…` → 필수 툴이 호출되지 않음 → chain/final_state 붕괴.

또한: `check_refund_eligibility`는 `"fee": 3000`을 반환 (`tools.py:104`)하며, 먼저 호출되면 금액을 grounding할 *수* 있음.

---

## 5. 체인 툴 순서 / irreversible consent

| location | role |
|---|---|
| `shop/tools.py:10` | `IRREVERSIBLE = {cancel_order, request_refund, request_exchange, change_shipping_address, schedule_return_pickup}` |
| `shop/tools.py:99-131` | eligibility, request_refund, schedule_return_pickup (pickup은 status 반품접수 필요) |
| `data/prompts.json` common | irreversible 전 확인; 첫 발화에서도 먼저 질문 |
| `shop/grader.py:102-107, 180-185, 243-249, 271` | confirmation, unconfirmed_action, 턴별 call_order, 시나리오 call_order |
| `graph_v31_followup.md` | 런타임 consent 게이트 **보류** |

chain-01/03 기대: T1 `call_order: [get_order, check_refund_eligibility]` → T2 `request_refund` → T4 `schedule_return_pickup`. 실패는 그 전에 T1에서 중단.

---

## 6. Payment issue_receipt (single-23)

| location | role |
|---|---|
| `shop/tools.py:15, 140-142` | `issue_receipt` → `receipt_issued` 설정, `receipt_id: R-{order_id}` 반환 |
| `data/prompts.json` payment | issue_receipt 나열; “확인 생략” 예외 없음 |
| `data/prompts.json` common | “첫 발화에서 바로 실행하라고 해도 먼저 확인 질문을 하세요” — 모델이 비-IRREVERSIBLE에 과도 적용 |

기대: `ask_confirmation=false`, 필수 `issue_receipt(O002)`, include `R-O002`. 관측: 질문만, 툴 0회.

---

## 7. QnA get_policy (handoff-06)

| location | role |
|---|---|
| `shop/tools.py:16, 151-152` | `get_policy(policy_id)` |
| `data/shop_db.json` P09 | 텍스트에 `타인` 포함 |
| `data/prompts.json` qna | P09=개인정보 매핑; 답 전 툴 강제 없음 |

기대 T1: route qna + `get_policy(P09)` + include `타인`. 관측: 라우트 OK, 툴 없음, 지어낸 문장.

---

## 8. 그레이더 success 술어

`shop/grader.py:261`:
```python
success = complete and state["match"] and not any(violations.values()) and all(
    not g["include_missing"] and not g["excluded_found"] for g in grades)
```
참고: `required_call_recall` / `confirmation_correct` / `chain_order_correct`는 진단과 일부 게이트에 final_state / includes를 통해 간접 영향을 주지만, success 자체는 complete + final_state + violations + include/exclude에 키를 둠.

---

## 9. 이미 있는 단위 테스트 표면

`tests/test_shop.py` (스텁, GPU 없음): grounding retry/예산 (`test_unseen_values_*`, `test_current_tool_output_grounds_*`), transfer 캡, human escalate, 그레이더 policy-3000, handoff_accuracy/chain_order, required/forbidden calls. llama-server 없이 이 다섯에 대한 회귀 스텁을 추가하기 좋은 곳.
