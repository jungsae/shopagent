# grade_summary — graph31 공통 실패 (변경 전)

스냅샷 날짜: 2026-10-25 (Asia/Seoul).  
주요 런: `g12-native-graph31-r{1,2,3}`.  
추가 스택 (graph31-r1만): Clef (`clef-g12-graph31-r1`), Omni (`jev-g12-graph31-r1` 폴더명 — **Omni**/Jev-Omni 로컬 스택, TypeSafe Jev API 아님), Pplx (`pplx-g12-graph31-r1`).  
출처: `runs/*/results.json` → `scenarios[]` + `grade`. 코드 수정 없음; GPU 없음.

범례: recall = `required_call_recall`; conf = `confirmation_correct`; miss = `include_missing`; excl = `excluded_found`; hallu = `hallucinated_values`; viol = 턴별 위반 카운트; chain = 턴 또는 시나리오 `chain_order_correct`; handoff = 시나리오 `handoff_accuracy`.

---

## chain-01 (환불 체인 O004)

| run | success | complete | chain | handoff | final_state | notes |
|---|---|---|---|---|---|---|
| g12-native-r1 | False | True | False | 1.0 | mismatch (status가 배송완료에 머묾, 반품접수 없음) | r1–r3 동일 패턴 |
| g12-native-r2 | False | True | False | 1.0 | same | |
| g12-native-r3 | False | True | False | 1.0 | same | |
| Clef | False | True | False | 1.0 | same | 이후 턴에서 부분 회복 |
| Omni | False | True | False | 1.0 | same | 부분 회복 |
| Pplx | False | True | False | 1.0 | same | 부분 회복 |

### g12-native-graph31-r1/r2/r3 (동일)

| turn | route | recall | conf | chain | miss | excl | hallu | viol | calls | ungrounded | reply snippet |
|---:|---|---:|---|---|---|---|---|---|---|---|---|
| 0 | order_delivery | 1.0 | True | True | [] | [] | [] | all 0 | get_order | [] | 주문번호 O004…상태는 '배송완료'…2026-10-20… |
| 1 | refund_exchange | 0.0 | False | **False** | [] | [] | [] | all 0 | [] | **['3000원']** | 확인된 정보만으로 날짜나 금액을 안내하기 어렵습니다… |
| 2 | refund_exchange | 0.0 | True | True | [] | [] | [] | all 0 | [] | [] | …직접 반품 처리를…권한이 없습니다. 반품/교환 담당 부서로 연결… |
| 3 | refund_exchange | 0.0 | False | True | [] | [] | [] | all 0 | [] | [] | 반품/교환 담당 부서로 연결해 드렸습니다… |
| 4 | refund_exchange | 0.0 | True | True | ['2026-10-23'] | [] | [] | all 0 | [] | [] | …수거 예약 절차를 도와드릴 예정입니다. |

### 추가 스택 (동일하게 실패)

- **모든 스택에서 T1 동일**: routes=refund_exchange, recall=0, conf=False, chain=False, calls=[], ungrounded=['3000원'], 고정 grounding 폴백 응답.
- Clef/Omni/Pplx 이후 턴: 때로 `check_refund_eligibility` / 확인 질문 / `schedule_return_pickup`을 시도하나, pickup 전에 `request_refund`가 실행되지 않음 → pickup 오류; T4에서 miss `2026-10-23`; final_state는 여전히 배송완료.

---

## chain-03 (환불 체인 O011)

모든 스택에서 chain-01과 같은 형태 (T1에서 `3000원` grounding 중단, 연쇄 실패, final_state가 배송완료에 머묾). Native r1–r3 응답은 O011로 chain-01을 거울처럼 반복. Omni/Clef/Pplx도 같은 연쇄로 실패.

---

## single-18 (2턴 환불 O004)

| run | success | complete | chain | handoff | final_state |
|---|---|---|---|---|---|
| g12-native-r1/r2/r3 | False | True | True | null | mismatch (반품접수 없음) |
| Clef / Omni / Pplx | False | True | True | null | **match** (툴 실행됨) 이지만 miss로 여전히 실패 |

### g12-native (r1=r2=r3)

| turn | route | recall | conf | miss | hallu | calls | ungrounded | reply snippet |
|---:|---|---:|---|---|---|---|---|---|
| 0 | refund_exchange | 0.5 | False | [] | [] | get_order | **['3000원']** | 확인된 정보만으로 날짜나 금액을 안내하기 어렵습니다… |
| 1 | refund_exchange | 0.0 | False | ['검수'] | [] | get_order, check_refund_eligibility | [] | …반품비 3,000원…반품 신청을 진행할까요? (request_refund 없음) |

### Clef / Omni / Pplx

| turn | route | recall | conf | miss | calls | reply snippet |
|---:|---|---:|---|---|---|---|
| 0 | refund_exchange | 1.0 | True | [] | get_order, check_refund_eligibility | …반품비 3,000원…반품 신청을 진행할까요? |
| 1 | refund_exchange | 1.0 | True | **['검수']** | request_refund | …반품 신청이 완료…환불 금액은 52,000원… (검수 누락) |

즉 Omni/Clef/Pplx 실패는 **응답 문구** (`reply_must_include: 검수`) 문제이며, 툴/상태가 아님. Native는 더 이른 grounding에서 실패.

---

## single-23 (issue_receipt O002)

| run | success | complete | route | recall | conf | miss | hallu | calls | final_state |
|---|---|---|---|---:|---|---|---|---|---|
| g12-native-r1/r2/r3 | False | True | payment ✓ | 0.0 | True* | ['R-O002'] | [] | [] | receipt_issued false |
| Clef | False | True | payment ✓ | 0.0 | True | ['R-O002'] | [] | [] | same |
| Omni | False | True | payment ✓ | 0.0 | True | ['R-O002'] | [] | [] | same |
| Pplx | False | True | payment ✓ | 0.0 | False | ['R-O002'] | [] | [] | same |

\*Native conf=True인 이유: expect `ask_confirmation=false`인데 응답이 부드러운 질문을 하고, 일부 스택에서는 여전히 confirmation으로 채점됨; Pplx는 conf=False (명시적 질문).  
응답 (native/Clef/Omni): `주문번호 O002에 대한 영수증 발급을 도와드리겠습니다. 영수증을 발급해 드릴까요?` — **`issue_receipt`를 절대 호출하지 않음**.

Clef / Omni / Pplx graph31-r1에서도 실패.

---

## handoff-06 (product → qna P09)

| run | success | complete | handoff_acc | T0 | T1 |
|---|---|---|---|---|---|
| g12-native-r1/r2/r3 | False | True | **1.0** | product, recall=1.0, get_reviews, miss=[] | qna, recall=**0.0**, calls=**[]**, miss=**['타인']** |
| Clef / Omni / Pplx | False | True | 1.0 | same OK | same: get_policy 없음, miss=['타인'] |

T0 응답 OK (품질 언급).  
T1 응답은 툴 없이 개인정보 정책 문장을 지어냄; 기대는 `get_policy(P09)`이며 그 텍스트에 `타인` 포함. final_state match=true (상태 변경 없음). 위반(violations) 전부 0.

Clef / Omni / Pplx graph31-r1에서도 실패.

---

## 스택 간 공통성

| id | native r1–r3 fail | Clef | Omni | Pplx |
|---|---|---|---|---|
| chain-01 | yes (동일) | yes | yes | yes |
| chain-03 | yes (동일) | yes | yes | yes |
| single-18 | yes (grounding) | yes (miss 검수) | yes (miss 검수) | yes (miss 검수) |
| single-23 | yes | yes | yes | yes |
| handoff-06 | yes | yes | yes | yes |

다섯 시나리오 모두 스택 간 **공통 실패**. 지배적 공유 메커니즘: (1) 툴 전에 정책 수수료 `3000원`에 대한 런타임 grounding 중단, (2) 필수 툴 없이 전문 에이전트가 응답 (`issue_receipt`, `get_policy`), (3) 부차적 reply-include 공백 (`검수`, `타인`, 수거 날짜).
