# Change C: 로컬 우선 최소 수정

날짜: 2026-10-28 (Asia/Seoul). 기존 `docs/failfix-before-20261028-C/` snapshot을 기준으로 현재 tree에 적용했다. snapshot은 재생성하지 않았고 기존 failfix 폴더와 `runs/`는 보존했다.

## 변경

- `robust-03`: `shop/agents.py`의 LocalLLM과 Agents.call 기록 모두 `max_tokens`를 512에서 1024로 올렸다. 긴 FAQ 답변의 잘림을 줄이려는 조치다. 생성 시간이 조금 길어지고 decode 작업량이 늘 수 있으며 GPU rebench 전 품질 개선을 보장하지 않는다.
- `single-19`: `shop/grader.py`의 value_tokens에서 O###, R-O###, TRK#########의 `\b`를 ASCII 영숫자 경계로 바꿨다. `O011를`, `O011에`도 사용자 근거에 포함된다. 날짜/금액 처리는 유지하며 근거 없는 O999는 계속 hallucinated_value로 잡는다. 기존 결과를 다시 채점하거나 덮어쓰지 않았다.
- `single-29`, `handoff-08`: P05에 출고 전의 결제완료/상품준비중 주문만 주소 변경 가능하고 출고 후 불가하다는 문구를 넣었다.
- `single-16`, `policy-01`: common/refund_exchange에 조회와 자격 확인은 동의 없이 수행하고, 가능 여부/기한 경과 예외 문의는 get_order와 check_refund_eligibility부터 호출하도록 명시했다. 자격 미달은 `불가`라고 설명하고 예외 승인이나 상담원 전환으로 대체하지 않도록 했다.
- refund_exchange에서 최신 사용자 발화에 주문번호와 가능/신청하지/예외/환불/교환/반품 의도가 있고 `동의`가 없으면, 도구 없는 답변에 기존 force-tool 재시도를 1회 적용한다. 현재 턴에 누락된 get_order/check_refund_eligibility만 SCOPES 안에서 요구하며 기존 step 제한을 유지한다. 도구를 직접 실행하거나 성공을 보장하는 장치는 아니다. 소유자 확인과 비가역 행동의 후속 동의는 계속 필요하다.
- `single-20`: 교환 자격 조회 뒤 주문번호/옵션을 명시한 확인 질문을 하고 다음 사용자 턴의 명시 동의 뒤에만 request_exchange를 호출하도록 prompt를 보강했다. 별도 교환 상태 머신은 추가하지 않았다.

## 로컬 검증

기존 114개 테스트가 먼저 통과했다. `tests/test_shop.py`에 7개 테스트를 추가하고 기존 mock/streaming 검증을 확장했다.

- P05 get_policy 출력의 `출고`와 허용 상태 확인.
- 조사와 붙은 주문/영수증/송장 ID, ASCII 영숫자 접합 거부, 날짜/금액 유지 확인.
- GraderTests의 scenario 사용자 근거 경로로 O011 재언급은 허용하고 O999는 거부.
- single-16/policy-01을 mock으로 실행하여 조회 도구 2개, 재시도 1회, `불가`, DB 불변 및 시나리오 채점 확인.
- 교환 의도의 누락 조회만 재요구, 이미 조회한 경우/동의 턴/주문번호 없음/다른 담당/최신 발화 기준의 비발동 확인.
- 조회/거절/교환 확인 prompt 계약 및 MockTransport의 실제 요청 한도와 Agents 기록 한도 1024 확인. 설치된 ChatOpenAI는 HTTP body에 `max_completion_tokens`로 직렬화한다.

실행: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q`

결과: **121 tests, OK**. `llama-server` 프로세스 0 확인. Upstage HTTP, GPU, 실제 API 키는 사용하지 않았다.

## 남은 검증

**GPU E2E not run - user must ask before rebench. GPU E2E는 실행하지 않았으며 사용자가 요청하기 전에는 rebench하지 않는다.**

토큰 한도 이후 robust-03 완주 여부, 실제 모델의 조회/거절 문구, 교환 확인/다음 턴 실행/final_state, P05 답변의 `출고`, routing/handoff는 GPU 검증이 필요하다. single-19는 채점기 오탐 수정이며 답변 모델의 전반적인 grounding 개선을 뜻하지 않는다. force-tool 재시도도 모델이 무시할 수 있고 기존 step/tool 예산을 소비한다. `chain-01` native 다중 턴 체인은 이번 변경으로 해결했다고 판단하지 않는다.

향후 요청 시 제안할 run 이름만 기록한다. 폴더는 생성하지 않았다.

- `clef-g12-graph31-fixC-r1`
- `pplx-g12-graph31-fixC-r1`
- `jev-g12-graph31-fixC-r1` (Omni)
- `g12-native-graph31-fixC-r1`
