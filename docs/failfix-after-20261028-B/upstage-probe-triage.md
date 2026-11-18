# Upstage 유료 probe 전 fixB 잔여 실패 분류

날짜: 2026-10-28 (Asia/Seoul). 무료 준비만 수행하며 HTTP, GPU, 실키를 사용하지 않는다.

근거: 기존 `runs/clef-g12-graph31-fixB-r1/results.json`, `runs/pplx-g12-graph31-fixB-r1/results.json`, `runs/jev-g12-graph31-fixB-r1/results.json`, `runs/g12-native-graph31-fixB-r1/results.json`. Omni의 폴더 이름은 `jev-g12`이다. 기존 결과는 읽기만 했으며 재실행하지 않았다.

Mix1은 cloud Decide + local Gemma reply, Mix2는 local decide + cloud reply이다. 아래 관측은 fixB 결과이고, 비용 낭비 가능성과 권고는 그 관측에 따른 가설이다. cloud로 바꾸면 해결된다는 증거는 아직 없다.

| ID | fixB에서 무엇이 실패했나 | 원인 영역 | Mix1 반복의 비용 가치 | Mix2 반복의 비용 가치 | 권고 |
| --- | --- | --- | --- | --- | --- |
| single-16 | 모든 config에서 반품 가능 여부만 조회하는 요청에 `불가`가 빠지고 tool recall이 약 0.5이다. | reply/tool/필수 문구. 라우팅은 refund_exchange로 맞아도 조회·자격 확인과 거절 설명이 부족하다. | 같은 Gemma 답변·도구 누락을 다시 볼 가능성이 높아 라우팅 비용 낭비 우려가 크다. | 답변 모델 차이를 볼 수 있지만 두 조회 도구와 거절 기준부터 정리해야 한다. | fix-local-first. get_order + check_refund_eligibility, 조회만 유지, `불가` 설명을 확인한 뒤 Mix2 후보로 남긴다. |
| single-29 | 모든 config의 정책 답변에서 `출고`가 빠진다. Clef는 qna 대신 order_delivery로 보낸다. | 주로 reply/문구, Clef에는 routing 문제도 있다. | Clef의 첫 라우팅 비교에는 가치가 있으나 `출고` 누락은 그대로 남을 수 있다. | 정책 문구 비교는 가능하나 local decide가 Clef이면 오분류도 유지될 수 있다. | 문구는 fix-local-first. smoke 이후 소수 include-in-paid-probe 후보로 첫 라우팅만 분리 평가한다. |
| single-19 | Clef/pplx/Omni dual에서 확인하지 않은 o011을 만들어 내며 native는 통과한다. | reply grounding. | local Gemma를 유지하므로 같은 날조 재현에 비용을 쓸 가능성이 높다. | cloud reply의 grounding 비교에는 의미가 있으나 검증·재시도 경로를 먼저 확인해야 한다. | fix-local-first. 주문번호 근거 검사 후 별도 승인된 Mix2 probe 후보로 둔다. |
| single-20 | pplx/Omni/native에서 교환 확인 질문과 final_state가 실패한다. Clef는 통과한다. | reply/tool consent. | routing만 바꾸면 확인·동의·실행 흐름 실패가 남을 가능성이 높다. | reply와 tool call 비교는 유효하지만 cloud도 동의 게이트를 지켜야 하므로 반복만으로 해결되지 않는다. | fix-local-first. 확인 질문, 다음 턴 명시 동의, request_exchange와 final_state를 먼저 점검한다. |
| handoff-08 | pplx/Omni/native에서 handoff 뒤 답변에 `출고`가 빠진다. Clef는 통과한다. | reply/문구 + handoff 경로. | 경로 비교에는 일부 가치가 있으나 문구 누락을 Decide 실패로 해석하면 비용 낭비다. | cloud reply 비교 후보지만 local handoff 경로의 영향도 남는다. | 문구는 fix-local-first. smoke 이후 include-in-paid-probe 후보로 경로와 문구 점수를 나눠 본다. |
| policy-01 | pplx/Omni/native에서 기한이 지난 주문의 예외 환불 거절에 `불가`가 빠지고 도구 사용이 적다. Clef는 통과한다. | route+reply/tool. 관측상 refund_exchange 선택만 맞아도 자격 확인과 거절이 실패할 수 있다. | 라우팅만 평가하는 제한된 가치는 있지만 조회·거절 누락을 반복할 가능성이 높다. | 예외 거절·도구 사용 비교는 가능하나 정책·동의 검증을 먼저 고정해야 한다. | fix-local-first. 이후 소수 include-in-paid-probe 후보로 두되 route와 거절·tool 평가를 분리한다. |
| robust-03 | Clef/pplx/native에서 `LLM output token limit reached`로 끝난다. Omni는 통과한다. | reply/local max_tokens. 경로에 따라 출력량도 달라질 수 있다. | local Gemma의 출력 제한을 유지한 채 Decide 비용만 내면 같은 중단이 재현될 수 있다. | cloud reply의 제한은 별도 설정이라 단순 비교로 Upstage 품질을 판단할 수 없다. | fix-local-first 또는 probe에서 제외. 로컬 토큰 제한을 고치거나 알려진 한계로 기록하고 Upstage 책임으로 돌리지 않는다. |
| chain-01 | native에서만 여러 턴의 반품·수거 체인이 무너져 final_state가 실패한다. dual은 통과한다. | reply/native 다중 턴 상태·도구 실행. | 이미 dual이 통과하므로 Mix1 Decide 차이를 보는 우선순위가 낮고 긴 체인만큼 호출 비용도 늘어난다. | reply/state 비교는 가능하지만 native 전용 실패를 재검증하는 데 먼저 과금할 이유는 적다. | fix-local-first. native 상태·동의 흐름을 확인하고 Mix1 decide probe에서는 낮은 우선순위로 둔다. |

유료 실행 승인을 받으면 Mix1 paid smoke 3개(single-01, single-07, single-18)부터 진행한다. Mix1에서는 라우팅만 과금되고 답변은 local Gemma이므로 대부분의 잔여 reply/tool/문구 실패는 먼저 로컬에서 고치거나 알려진 실패로 받아들인 뒤 probe 비용을 써야 한다. 이후 예산·결과를 보고 single-29, handoff-08, policy-01 중 라우팅 가설이 분명한 소수만 선택한다. Mix2는 별도 승인과 model·단가 확정 전까지 보류한다.

`python -m shop.upstage_dryrun`은 첫 사용자 턴을 `human: ...`로 만들어 실제 첫 routing item의 body 연결만 증명한다. 후속 턴은 최근 user 발화 최대 10개만 누적하며 assistant/tool 응답과 실제 handoff 상태가 없다. 따라서 후속 payload는 구조 점검용이고 실제 다중 턴 라우팅 정확도나 실패 해결의 증거는 아니다. `post_systemone`은 호출하지 않으며 키·ALLOW 설정과 무관하게 JSON만 만든다.

## Change C 후

- 코드/데이터 반영: robust-03의 max_tokens 1024, single-19의 조사 접합 ID 채점 오탐, single-29/handoff-08의 P05 `출고` 문구, single-16/policy-01의 조회 도구 재요구와 `불가` prompt, single-20의 교환 확인 prompt를 수정했다. single-19의 기존 o011 관측은 사용자 근거 추출 실패에 따른 오탐이므로 모델 날조로 단정하지 않는다.
- 로컬 확인: 유닛 121개 통과. 실제 답변의 잘림/거절/확인/후속 실행과 final_state, `출고` 포함, routing/handoff는 GPU E2E로 확인해야 한다. single-20은 prompt와 첫 조회 재시도 수준이며 해결 보장이 없다.
- 열린 항목: chain-01 native 다중 턴 체인, single-29의 Clef routing 등은 별도 검증/분석이 남는다. 유료 Mix1/Mix2로 해결된다는 증거도 없다.

상세: [Change C CHANGELOG](../failfix-after-20261028-C/CHANGELOG.md). GPU E2E는 실행하지 않았다. 사용자가 요청하기 전에는 rebench하지 않으며 기존 `runs/`도 수정하지 않는다.
