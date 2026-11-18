# ShopAgent graph v3.1 checkpoint

CPU-only local changes; no live inference or GPU remeasurement.

Files changed:
- `shop/graph.py`: deterministic `human` -> `escalate` node calls `Shop.invoke("escalate_to_human", ...)`, using the latest user message for reason/summary, records the tool exchange, and returns a fixed confirmation or failure reply. Native and decision routing use this path. No specialist LLM or HITL interrupt is needed.
- `shop/graph.py`: `MAX_TRANSFER_BACK_PER_TURN = 3` configures the transfer cap. Transfers must still be alone and without arguments; the rejection message includes the configured cap.
- `shop/graph.py`: final replies use the existing grader's ISO/Korean date and won-amount normalization. Evidence is user messages in the supplied history plus domain-tool outputs from the current turn. Numeric monetary tool fields are included. Assistant drafts, tool arguments, system policy, and previous-turn tool outputs alone do not ground values.
- `shop/graph.py`: unsupported values trigger at most one correction call, with a system note. Rejected drafts stay out of `visible`; raw LLM traces retain them. A failed correction, tool calls during correction, or no remaining step budget yields a fixed reply without numbers and `grounding_failed=True`. Corrections cannot execute tools. `grounding_retries` and `ungrounded_values` expose the current result. Fixed replies have null final-reply TTFT.
- `shop/tools.py`: copy flattened before/after DB snapshots so escalation list appends appear in tool diffs and later appends cannot alter earlier trace records.
- `tests/test_shop.py`: cover native/decision human routes, rerouting, escalation faults and trace snapshots, transfer cap/configuration/validation, date and amount grounding, evidence boundaries, single retry, mutation prevention, and budget exhaustion. Mocked handoff-05/policy-06/policy-07 pass the existing scenario grader.

Validation:
- Baseline: 82 unittest tests passed.
- Final: 92 unittest tests passed using `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q` from `shopagent/`.
- Existing limits remain eight steps and six tool calls. The step budget can stop a turn before three transfers; the cap boundary is tested directly at the tools node. Human escalation also respects these budgets.
- `parallel_tool_calls=False` remains unchanged. No dependencies installed, servers started, models downloaded, or commits made.

Follow-ups: see `graph_v31_followup.md`. This checkpoint does not claim improved live-model pass rates.
