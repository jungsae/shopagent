# ShopAgent 런 폴더 작명 풀이

기존 `runs/*` 이름은 바꾸지 않는다. 이 문서는 이름만 읽을 때 쓰는 사전이다.

## 기본 형식

```text
runs/<config>[-nglN]-r<repeat>/
```

`run.py --full --config <config> --repeat <N>` 의 기본 폴더는 `runs/<config>-r<N>/` 이다.
`--name` / `--tag` 로 폴더 이름을 직접 정할 수 있고, 이미 있는 폴더는 거부한다(덮어쓰기 없음).

`--repeat N` 은 **몇 번 돌릴지**가 아니라 **몇 번째 반복인지**(1-based index)다. `--attempts K`가 없으면 한 명령은 시나리오별 한 시도를 실행한다.

## 조각 뜻

| 조각 | 의미 |
| --- | --- |
| `g12` | Gemma 4 12B (에이전트 LLM) |
| `native` | 판단 모델 없음. Gemma가 라우팅과 답변을 모두 담당 |
| `clef-g12` | Clef 판단 모델 + Gemma 12B |
| `jev-g12` | Jev-Omni(로컬) 판단 모델 + Gemma 12B |
| `pplx-g12` | pplx-decider 판단 모델 + Gemma 12B |
| `ngl0` / `nglN` | Gemma `-ngl` 을 기본값과 다르게 둔 참고 실행. `reference_offload` 로 기록됨 |
| `r1`, `r2`, `r3` | 같은 config의 1·2·3번째 반복(repeat index) |
| `smoke-…` | smoke 3시나리오만 (`run.py` 기본, `--full` 없음) |
| `calibration-…` | 짧은 동시 상주/경합 측정 (`calibrate.py`). 60시나리오 본측정 아님 |
| `unit-…` | 유닛/짧은 검증용 태그 |

## 예시

| 폴더 | 풀이 |
| --- | --- |
| `g12-native-r1` | Gemma 12B native, 1번째 full(66) 반복 |
| `g12-native-r2` | 위와 동일 설정의 2번째 반복 |
| `g12-native-ngl0-r1` | Gemma `-ngl 0`(CPU 참고) native 1번째 full |
| `clef-g12-r1` | Clef + Gemma, 1번째 full |
| `jev-g12-r1` | Jev + Gemma, 1번째 full |
| `pplx-g12-r1` | pplx + Gemma, 1번째 full |

## config 기본 Gemma `-ngl` (Stage 3c)

| Config | Gemma `-ngl` | Judge |
| --- | --- | --- |
| `g12-native` | 99 | 없음(Gemma 자체) |
| `clef-g12` | 16 | `-ngl 99` |
| `pplx-g12` | 4 | `-ngl 99` |
| `jev-g12` | 0 | `-ngl 99` |
| `solar-pro4` | 해당 없음 | Upstage Solar Pro 4 API 단일 모델, 모든 노드 |

기본과 다른 `-ngl` 은 이름에 `nglN` 을 넣거나 `--name` 으로 구분한다. 성공률 집계 때 native 3반복(`r1`–`r3`)과 참고 offload(`ngl0` 등)는 섞지 않는다.

## 관련 코드

- 폴더 규칙: `run.py` (`--config`, `--repeat`, `--name`/`--tag`)
- config 표: `shop/servers.py` 의 `CONFIGS`, README Stage 3c


## v2 LangGraph 동의 대기

현재 데이터는 66개 시나리오·109턴이다. `--graph v2`는 담당자 답변 → `decide_action` → graph state 저장 → interrupt → 같은 thread의 Command(resume=고객 발화) → consent_judge → 저장 인자 실행 → report 순서로 동작한다. 기본 폴더는 `runs/<config>-graph2-r<N>/`, 교정 규칙을 켠 참고 실행은 `runs/<config>-graph2-assist-r<N>/`이다. v1과 v2, assist on/off는 별도로 집계한다. 기존 결과의 이름과 내용은 유지하며 데이터/프롬프트 fingerprint로 세대를 구분한다.


## 새 이름 규칙 (Saehan)

새 run 폴더 이름·label·metric 이름은 이름만 읽어도 의미가 드러나야 한다. 한 글자 코드나 설명 없는 축약은 사용하지 않는다. 예를 들어 `full-v2-decide-off`, `full-v2-decide-assist-on`, `full-v2-decide-off-attempts-3`처럼 쓴다. `v2d` 같은 새 축약은 쓰지 않는다. 기존 폴더 이름과 기본 호환 이름은 유지하며 기존 `runs/` 폴더를 변경하거나 이름을 바꾸지 않는다.

`--attempts K`는 같은 run 폴더에서 시나리오마다 K번 순차 시도하며 `--repeat N` 인덱스와 별개다. K=1은 기존 results 형식과 파일명을 유지한다. K>1은 결과에 `attempts=K`, 각 행에 같은 시나리오 id와 `attempt_index=1..K`를 남긴다. 첫 시도는 `traces/<id>.jsonl`, 두 번째부터 `traces/<id>-attempt-<N>.jsonl`이다. smoke 텍스트는 첫 `<id>-trace.txt`, 이후 `<id>-attempt-<N>-trace.txt`다. 예: `single-18.jsonl`, `single-18-attempt-2.jsonl`. 파일명은 서로 충돌하지 않는다. pass^k 지표 이름은 `pass_at_1_mean`, `pass_hat_k_per_scenario`, `pass_hat_k_overall`이며 `attempts`로 K를 함께 표시한다.


## 노드 정책·확인 카드·조회 게이트 세대

`2026-11-16-node-policy-confirmation-card-gating`은 Step 2 데이터 버전이다. 새 이름은 `full-v2-node-policy-confirmation-off`, `full-v2-node-policy-confirmation-assist-on`처럼 동작을 읽을 수 있게 쓰고 기존 폴더는 그대로 둔다. 비교할 때 data_version, dataset/prompts fingerprint와 assist를 함께 확인한다.

v2_required_calls는 필수 호출이고 v2_reference_calls는 성공에 영향 없는 참고 호출 재현율이다. get_order는 single-07/14..20/34..36, handoff-01, policy-01/03/08의 T0와 handoff-03 T1, chain-01..06 T1/T3에서 참고로 내린다. single-35 T1은 get_order/check_refund_eligibility 모두 참고다. get_policy는 single-26..30 T0, handoff-04 T0, handoff-06/08 T1, robust-08 T0에서 참고다. robust-05는 search_faq 이름만 필수이고 query는 참고다. robust-01/02/03의 재시도 두 번은 필수 유지다. 순서는 chain의 request_refund→schedule_return_pickup과 single-31/32/33 T0의 get_order→check_cancel_items만 남긴다.

reason 필수 대상은 single-18 T0와 single-34/35/36 T0 및 chain-01/03/05 T1의 단순변심, single-19 T0 및 chain-02/04/06 T1의 상품불량이다. 사유가 없는 요청은 사유 게이트를 추가하지 않는다.

정책 매핑은 order_delivery=P04/P05/P08/P09, refund_exchange=P01/P02/P03/P09/P10, payment=P06/P07/P12, qna=P01..P12, product=없음, human=P11, 라우터(router)=없음이다. get_policy와 같은 DB 원문을 주입하고 policy_context에 방문 노드별 ID를 남긴다. 기존 v1 프롬프트 키는 바뀌지 않는다. v2 변형에서 제거한 정확한 정책 문장은 [README Step 2](README.md#v2-step-2-조회-게이트와-정책확인-카드)에 열거한다.

여섯 실행 행동은 모델 원문 뒤에 DB 상품명·옵션·수량·금액·백엔드 DRY RUN 비용/환불 또는 주소/수거일 확인 카드를 붙인다. confirmation_card는 별도 저장하고 pending.message에는 전체 문구를 저장한다. 복제 DB의 계산은 agent 도구 호출·횟수에 들어가지 않는다.

동의 consent가 있는 턴은 그래프 밖 simulated_customer가 원래 고객 요청과 전체 제안을 비교한다. approve면 원래 대본, reject/change면 모델 발화를 전달한다. forced customer_response 호출과 지연시간은 기록하지만 도구/모델 한도는 소비하지 않는다. judge accuracy는 실제 고객 결정과 비교하며 원래 실행 게이트는 유지한다. single-34/35/36의 응답은 고정 대본이다.

`rescore.py --v2-report`는 stdout만 출력하고 파일을 쓰지 않는다. 저장 기록의 채점 변화만 보여주며 정책 주입·확인 카드·시뮬레이터의 런타임 효과를 재현하지 않는다. policy_context가 없는 이전 결과는 기존 grounding을 유지한다.
