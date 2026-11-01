# ShopAgent graph v3.1 follow-ups

The optional irreversible-tool confirmation gate is deferred. A correct gate must bind explicit user consent to the requested action, order, and arguments, and distinguish approval from refusal, quoted text, and unrelated older consent. Reusing the grader's scenario `consent` labels at runtime would leak evaluation answers. The current tool schemas already request confirmation, but the graph does not enforce it.

A separate change should define runtime consent state for `cancel_order`, `request_refund`, `request_exchange`, `change_shipping_address`, and `schedule_return_pickup`, then test approved, refused, ambiguous, stale, and mismatched confirmations before enabling execution gating.

The eight-step budget remains independent of the three-transfer cap; an ordinary turn reaches the step limit before a third transfer can execute. Any step-budget change needs separate validation. Human escalation can also be blocked by an exhausted step/tool budget.

Grounding is a rule-based date/won-amount check, not semantic verification. It does not verify that a seen amount belongs to the right order, inspect prose accompanying pending tool calls, or cover every currency/number spelling. Current-turn tool outputs are trusted evidence; a value echoed by a tool can therefore count as grounded. A correction may omit required reply details or fall back rather than resolve the question. Live-model accuracy and latency remain unmeasured.

HITL interruption is not implemented; the deterministic mock escalation runs directly. No GPU runs or live-server follow-ups were performed.
