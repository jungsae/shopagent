# failfix-after-20261028-B — Change set B (tool-before-answer)

날짜: 2026-10-28 (Asia/Seoul).  
범위: Change set B만 (H2). Change set C / GPU 재벤치 미실행.  
기준 실패 런: `runs/g12-native-graph31-fixA-r1` 잔여 8실패 중 H2 타깃.

## 변경 요약

1. **`data/prompts.json`**
   - `common`: 되돌릴 수 없는 행동에만 확인 질문을 한정. `issue_receipt` / `get_payment` / `get_policy` / `get_reviews`는 확인 게이트 대상이 아니며 명확한 요청 시 즉시 호출.
   - `payment`: `issue_receipt` / `get_payment` / `list_payment_methods` 즉시 호출 안내.
   - `qna`: 사용자가 `P0x`/`P\d+`를 지정하면 답 전 `get_policy` 필수, 정책 문장 날조 금지.
   - IRREVERSIBLE 목록 자체는 변경하지 않음.

2. **`shop/graph.py`**
   - `force_tool_retries` 상태 추가.
   - 전문 에이전트가 tool_calls 없이 최종 텍스트를 내고, 최근 HumanMessage가 `P\d+` 또는 `영수증`과 매칭되며 해당 툴이 아직 이번 턴 trace에 없으면 SystemMessage로 1회 force-tool 재시도 (step < 8, grounding_retries==0일 때 grounding 검사보다 먼저).
   - 패턴에 bare `O\d+`는 넣지 않아 취소 등 IRREVERSIBLE 확인 흐름을 건드리지 않음.

3. **`tests/test_shop.py`**
   - Change B 회귀 스텁 추가 (아래 신규 테스트).
   - 기존 할루시네이션/grounding 가드 테스트는 유지.

4. **`shop/agents.py`**: 변경 없음 (해시 동일).

## 신규/추가 테스트

```
def test_tool_before_answer_prompt_contract(self):
def test_receipt_and_policy_force_tool_scenarios(self):
def test_force_tool_retries_once_and_keeps_grounding_guard(self):
def test_force_tool_uses_latest_user_and_specialist_scope(self):
def test_force_tool_respects_step_budget(self):
```

## 검증

```bash
cd /home/home/llama.cpp/bench/shopagent
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
```

결과: **Ran 98 tests — OK** (Codex 통과 후 executor가 unittest discover -s tests -q 로 재확인, ~5.5s).

## 잔여 리스크 / 범위 밖

- GPU E2E 미실행 → single-23 / handoff-06 / handoff-08 실모델 개선은 `g12-native-graph31-fixB-r1` 재벤치로 확인 필요.
- Change B 밖: single-12 (IRREVERSIBLE consent), handoff-03 (track_shipment), policy-01 (escalate/예외), handoff-07 (`list_payment_methods`는 프롬프트만·force-tool 패턴 없음), single-29 (이미 get_policy OK, reply `출고` 문구).
- force-tool은 모델이 재시도에서도 툴을 안 부르면 실패할 수 있음 (1회 한도).
- get_reviews는 프롬프트에서 확인 비대상으로 명시했으나 force-tool 트리거 패턴(P/영수증)에는 없음.

## 권장 다음 GPU 런 이름 (실행하지 말 것)

`runs/g12-native-graph31-fixB-r1`

기존 `runs/*` 덮어쓰기 금지. clef/pplx/Omni 재벤치는 별도 승인 후.
