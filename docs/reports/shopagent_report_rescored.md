# Shopagent 저장 실행 재채점 v2

모델 호출·다운로드·패키지 설치 없이 저장 results/JSONL만 사용했다. LangSmith tracing 두 변수를 false로 설정하고 API 키를 프로세스 환경에서 제거했다. 감사 훅으로 네트워크 연결, 외부 프로세스 실행, 금지된 자격 증명 파일 접근을 차단했다.

원본 results.json, 기존 보고서, 데이터·gold·trace의 SHA-256 보존을 확인했다. 최종 DB는 JSONL db_diff로 복원하고 도구 changes와 충돌이 없는지 및 원래 final_state 판정이 같은지 확인했다. 기존 도구 changes는 가변 리스트 append를 누락할 수 있어 최종 db_diff를 권위 있는 기록으로 사용했다. 차이는 각 결과의 rescore_replay_notes에 기록했다. 표의 old는 저장된 판정이며 new는 수정 grader의 판정이다.

## 정규화·동치 규칙 전체

- (a) 날짜: YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD, YYYY년 M월 D일, M월 D일, M/D, M.D, M-D를 달력 검증 후 ISO 날짜로 통일한다. 전각 문자는 NFKC로 변환한다. 연도·월 표기가 없는 숫자는 별점/평점/점수/rating/score 문맥이나 점·원·만·천·백·십·%·퍼센트 단위가 붙으면 날짜로 해석하지 않는다.
- (a) 명시된 연도를 보존한다. 생략된 연도는 scenario.base_year, scenario.reference_date, 데이터셋 기준일(2026-10-22) 순으로 정한다. 월/일에 따른 연도 넘김 추측은 하지 않는다.
- (a) 같은 날짜 정규화를 사용자/도구 근거, 응답의 hallucinated_value, reply_must_include, reply_must_not_include에 적용한다.
- (b) 동의어 표는 품절 / 재고없음 / 재고가없 / 재고없 및 불가 / 불가능 / 할수없 두 그룹이다. 공백과 활용 어미를 고려하되, 확인된 부정·예정·임박·불가피 표현은 동의어로 바꾸지 않는다. 재고 부족, 입고 예정, 어렵다, 권장하지 않는다 등은 추가하지 않는다.
- (b) 원화 금액: 쉼표·공백·전각 숫자를 정규화하고, 숫자와 내림차순 만(10000)·천(1000)·백(100)·십(10) 단위를 정수 원화로 계산한다. 52,000원 = 52000원 = 5만 2천원 = 5만2000원. 원 단위 없는 숫자, 다른 통화, 뒤집힌 단위, 단위 범위를 넘는 잔여 숫자는 동치 처리하지 않는다.
- (b) 금액 정규화는 응답 조건과 grounding 양쪽에 적용한다. 날짜·금액 경계를 보존하여 52000원이 152000원 내부에서 일치하지 않게 한다.
- (c) 미래 날짜를 계산만으로 허용하지 않는다. 현재 턴까지의 사용자 발화나 실제 도구 인자/출력에 해당 날짜가 있어야 한다. 도구 인자는 날짜만 근거로 추가하며 주문 ID·금액에는 확대하지 않는다. 기준일과 공통 프롬프트의 기존 근거 인정은 유지한다.
- 동의어 표에 해당하지 않는 조건은 기존 부분 문자열 매칭을 유지한다. 기존 NFKC·공백 제거·쉼표 제거·대소문자 정규화 및 성공 게이트는 유지한다. 필수 호출·라우팅 등 진단 지표를 새로운 성공 게이트로 추가하지 않는다.

## 검증

수정 전 grader로 저장된 420개 판정을 모두 재현했다. 수정 전 unittest 68개 통과. 수정 후 unittest 전체 82개 통과(추가 14개, 각 표기·긍정·부정 사례는 subTest로 검사). 요청한 `.venv/bin/python -m pytest -q tests`는 실행했으나 기존 .venv에 pytest가 없어 `No module named pytest`로 시작 실패했다. 설치 금지 조건을 준수했다.

## 구성별 성공률

g12-native는 r1-r3 총 180개를 합산한다. CPU 참고 실행은 별도 구성으로 구분한다.

| 구성 | old | new |
| --- | --- | --- |
| g12-native | 126/180 (70.0%) | 138/180 (76.7%) |
| clef-g12 | 45/60 (75.0%) | 48/60 (80.0%) |
| pplx-g12 | 42/60 (70.0%) | 48/60 (80.0%) |
| jev-g12 | 45/60 (75.0%) | 49/60 (81.7%) |
| g12-native-ngl0 (CPU 참고) | 45/60 (75.0%) | 50/60 (83.3%) |

## 실행별 성공률 및 hallucinated_value 개수

hallucinated_value는 시나리오별 턴 검출 개수의 합이다. 동일 값이 여러 턴에 나오면 각 턴을 센다.

| 실행 | old 성공 | new 성공 | old hallucinated_value | new hallucinated_value |
| --- | --- | --- | --- | --- |
| g12-native-r1 | 42/60 (70.0%) | 46/60 (76.7%) | 6 | 3 |
| g12-native-r2 | 42/60 (70.0%) | 46/60 (76.7%) | 6 | 3 |
| g12-native-r3 | 42/60 (70.0%) | 46/60 (76.7%) | 6 | 3 |
| clef-g12-r1 | 45/60 (75.0%) | 48/60 (80.0%) | 6 | 4 |
| pplx-g12-r1 | 42/60 (70.0%) | 48/60 (80.0%) | 9 | 2 |
| jev-g12-r1 | 45/60 (75.0%) | 49/60 (81.7%) | 5 | 2 |
| g12-native-ngl0-r1 | 45/60 (75.0%) | 50/60 (83.3%) | 6 | 3 |

## 범주별 성공률

| 구성 | 범주 | old | new |
| --- | --- | --- | --- |
| g12-native | chain | 3/18 (16.7%) | 9/18 (50.0%) |
| g12-native | handoff | 9/24 (37.5%) | 9/24 (37.5%) |
| g12-native | policy | 15/24 (62.5%) | 15/24 (62.5%) |
| g12-native | robust | 24/24 (100.0%) | 24/24 (100.0%) |
| g12-native | single | 75/90 (83.3%) | 81/90 (90.0%) |
| clef-g12 | chain | 1/6 (16.7%) | 2/6 (33.3%) |
| clef-g12 | handoff | 7/8 (87.5%) | 7/8 (87.5%) |
| clef-g12 | policy | 7/8 (87.5%) | 7/8 (87.5%) |
| clef-g12 | robust | 7/8 (87.5%) | 7/8 (87.5%) |
| clef-g12 | single | 23/30 (76.7%) | 25/30 (83.3%) |
| pplx-g12 | chain | 0/6 (0.0%) | 4/6 (66.7%) |
| pplx-g12 | handoff | 6/8 (75.0%) | 6/8 (75.0%) |
| pplx-g12 | policy | 7/8 (87.5%) | 7/8 (87.5%) |
| pplx-g12 | robust | 7/8 (87.5%) | 7/8 (87.5%) |
| pplx-g12 | single | 22/30 (73.3%) | 24/30 (80.0%) |
| jev-g12 | chain | 2/6 (33.3%) | 4/6 (66.7%) |
| jev-g12 | handoff | 6/8 (75.0%) | 6/8 (75.0%) |
| jev-g12 | policy | 7/8 (87.5%) | 7/8 (87.5%) |
| jev-g12 | robust | 7/8 (87.5%) | 7/8 (87.5%) |
| jev-g12 | single | 23/30 (76.7%) | 25/30 (83.3%) |
| g12-native-ngl0 (CPU 참고) | chain | 1/6 (16.7%) | 4/6 (66.7%) |
| g12-native-ngl0 (CPU 참고) | handoff | 5/8 (62.5%) | 5/8 (62.5%) |
| g12-native-ngl0 (CPU 참고) | policy | 6/8 (75.0%) | 6/8 (75.0%) |
| g12-native-ngl0 (CPU 참고) | robust | 8/8 (100.0%) | 8/8 (100.0%) |
| g12-native-ngl0 (CPU 참고) | single | 25/30 (83.3%) | 27/30 (90.0%) |

## 판정이 바뀐 모든 시나리오

b:amount는 원화 정규화, b:equivalence는 위 동의어 표다. 각 행의 턴 번호는 1부터 센다.

| 실행 | 시나리오 | old -> new | 원인 |
| --- | --- | --- | --- |
| g12-native-r1 | single-05 | fail -> pass | 턴 1 include_missing removed: 품절 (b:equivalence) |
| g12-native-r1 | single-09 | fail -> pass | 턴 1 include_missing removed: 2026-10-20 (a); 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-r1 | chain-01 | pass -> fail | 턴 2 hallucinated_values added: 2026-10-29 (a) |
| g12-native-r1 | chain-02 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a) |
| g12-native-r1 | chain-04 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-r1 | chain-05 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a) |
| g12-native-r2 | single-05 | fail -> pass | 턴 1 include_missing removed: 품절 (b:equivalence) |
| g12-native-r2 | single-09 | fail -> pass | 턴 1 include_missing removed: 2026-10-20 (a); 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-r2 | chain-01 | pass -> fail | 턴 2 hallucinated_values added: 2026-10-29 (a) |
| g12-native-r2 | chain-02 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a) |
| g12-native-r2 | chain-04 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-r2 | chain-05 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a) |
| g12-native-r3 | single-05 | fail -> pass | 턴 1 include_missing removed: 품절 (b:equivalence) |
| g12-native-r3 | single-09 | fail -> pass | 턴 1 include_missing removed: 2026-10-20 (a); 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-r3 | chain-01 | pass -> fail | 턴 2 hallucinated_values added: 2026-10-29 (a) |
| g12-native-r3 | chain-02 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a) |
| g12-native-r3 | chain-04 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-r3 | chain-05 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a) |
| clef-g12-r1 | single-05 | fail -> pass | 턴 1 include_missing removed: 품절 (b:equivalence) |
| clef-g12-r1 | single-09 | fail -> pass | 턴 1 include_missing removed: 2026-10-20 (a); 턴 1 hallucinated_values removed: 10월20일 (a) |
| clef-g12-r1 | chain-01 | pass -> fail | 턴 2 hallucinated_values added: 2026-10-29 (a) |
| clef-g12-r1 | chain-02 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a) |
| clef-g12-r1 | chain-05 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a) |
| pplx-g12-r1 | single-05 | fail -> pass | 턴 1 include_missing removed: 품절 (b:equivalence) |
| pplx-g12-r1 | single-09 | fail -> pass | 턴 1 include_missing removed: 2026-10-20 (a); 턴 1 hallucinated_values removed: 10월20일 (a) |
| pplx-g12-r1 | chain-02 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a); 턴 4 hallucinated_values removed: 2026-10-23 (a) |
| pplx-g12-r1 | chain-03 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |
| pplx-g12-r1 | chain-05 | fail -> pass | 턴 1 hallucinated_values removed: 10월15일 (a); 턴 4 hallucinated_values removed: 2026-10-24 (a) |
| pplx-g12-r1 | chain-06 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |
| jev-g12-r1 | single-05 | fail -> pass | 턴 1 include_missing removed: 품절 (b:equivalence) |
| jev-g12-r1 | single-09 | fail -> pass | 턴 1 include_missing removed: 2026-10-20 (a); 턴 1 hallucinated_values removed: 10월20일 (a) |
| jev-g12-r1 | chain-03 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |
| jev-g12-r1 | chain-06 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-ngl0-r1 | single-05 | fail -> pass | 턴 1 include_missing removed: 품절 (b:equivalence) |
| g12-native-ngl0-r1 | single-09 | fail -> pass | 턴 1 include_missing removed: 2026-10-20 (a); 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-ngl0-r1 | chain-04 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |
| g12-native-ngl0-r1 | chain-05 | fail -> pass | 턴 4 hallucinated_values removed: 2026-10-24 (a) |
| g12-native-ngl0-r1 | chain-06 | fail -> pass | 턴 1 hallucinated_values removed: 10월20일 (a) |

fail -> pass: 34개. pass -> fail: 4개. 설명되지 않은 fail -> pass FLAG: 0개.

FLAG 목록: 없음

## 남은 실패 유형

실패 시나리오에 여러 유형을 중복 부여했다. 필수 호출·라우팅은 기존 성공 게이트가 아닌 진단 지표다. 남은 실패는 자동 판정상 실패로, 실제 사용자 답변의 의미 평가는 추가 검토가 필요할 수 있다.

### final_state mismatch (31개)

g12-native-r1/single-12, g12-native-r1/single-23, g12-native-r1/handoff-05, g12-native-r1/policy-06, g12-native-r1/policy-07, g12-native-r2/single-12, g12-native-r2/single-23, g12-native-r2/handoff-05, g12-native-r2/policy-06, g12-native-r2/policy-07, g12-native-r3/single-12, g12-native-r3/single-23, g12-native-r3/handoff-05, g12-native-r3/policy-06, g12-native-r3/policy-07, clef-g12-r1/single-12, clef-g12-r1/single-20, clef-g12-r1/single-23, clef-g12-r1/policy-07, pplx-g12-r1/single-12, pplx-g12-r1/single-20, pplx-g12-r1/single-23, pplx-g12-r1/policy-07, jev-g12-r1/single-20, jev-g12-r1/single-23, jev-g12-r1/policy-07, g12-native-ngl0-r1/single-23, g12-native-ngl0-r1/chain-01, g12-native-ngl0-r1/handoff-05, g12-native-ngl0-r1/policy-06, g12-native-ngl0-r1/policy-07

### hallucinated_value (20개)

g12-native-r1/chain-01, g12-native-r1/chain-03, g12-native-r1/chain-06, g12-native-r2/chain-01, g12-native-r2/chain-03, g12-native-r2/chain-06, g12-native-r3/chain-01, g12-native-r3/chain-03, g12-native-r3/chain-06, clef-g12-r1/chain-01, clef-g12-r1/chain-03, clef-g12-r1/chain-04, clef-g12-r1/chain-06, pplx-g12-r1/chain-01, pplx-g12-r1/chain-04, jev-g12-r1/chain-01, jev-g12-r1/chain-04, g12-native-ngl0-r1/single-18, g12-native-ngl0-r1/chain-01, g12-native-ngl0-r1/chain-03

### handoff tool not called (15개)

g12-native-r1/handoff-05, g12-native-r1/policy-06, g12-native-r1/policy-07, g12-native-r2/handoff-05, g12-native-r2/policy-06, g12-native-r2/policy-07, g12-native-r3/handoff-05, g12-native-r3/policy-06, g12-native-r3/policy-07, clef-g12-r1/policy-07, pplx-g12-r1/policy-07, jev-g12-r1/policy-07, g12-native-ngl0-r1/handoff-05, g12-native-ngl0-r1/policy-06, g12-native-ngl0-r1/policy-07

### missing refusal phrase (3개)

g12-native-r1/policy-01, g12-native-r2/policy-01, g12-native-r3/policy-01

### missing required reply (43개)

g12-native-r1/single-23, g12-native-r1/single-29, g12-native-r1/handoff-03, g12-native-r1/handoff-06, g12-native-r1/handoff-07, g12-native-r1/handoff-08, g12-native-r2/single-23, g12-native-r2/single-29, g12-native-r2/handoff-03, g12-native-r2/handoff-06, g12-native-r2/handoff-07, g12-native-r2/handoff-08, g12-native-r3/single-23, g12-native-r3/single-29, g12-native-r3/handoff-03, g12-native-r3/handoff-06, g12-native-r3/handoff-07, g12-native-r3/handoff-08, clef-g12-r1/single-18, clef-g12-r1/single-23, clef-g12-r1/single-27, clef-g12-r1/handoff-06, clef-g12-r1/robust-03, pplx-g12-r1/single-07, pplx-g12-r1/single-18, pplx-g12-r1/single-23, pplx-g12-r1/single-29, pplx-g12-r1/handoff-06, pplx-g12-r1/handoff-08, pplx-g12-r1/robust-03, jev-g12-r1/single-07, jev-g12-r1/single-18, jev-g12-r1/single-23, jev-g12-r1/single-29, jev-g12-r1/handoff-06, jev-g12-r1/handoff-08, jev-g12-r1/robust-03, g12-native-ngl0-r1/single-18, g12-native-ngl0-r1/single-23, g12-native-ngl0-r1/single-29, g12-native-ngl0-r1/chain-01, g12-native-ngl0-r1/handoff-06, g12-native-ngl0-r1/handoff-08

### required call missing / wrong arguments (59개)

g12-native-r1/single-12, g12-native-r1/single-23, g12-native-r1/chain-01, g12-native-r1/chain-03, g12-native-r1/chain-06, g12-native-r1/handoff-03, g12-native-r1/handoff-06, g12-native-r1/handoff-07, g12-native-r1/handoff-08, g12-native-r1/policy-01, g12-native-r2/single-12, g12-native-r2/single-23, g12-native-r2/chain-01, g12-native-r2/chain-03, g12-native-r2/chain-06, g12-native-r2/handoff-03, g12-native-r2/handoff-06, g12-native-r2/handoff-07, g12-native-r2/handoff-08, g12-native-r2/policy-01, g12-native-r3/single-12, g12-native-r3/single-23, g12-native-r3/chain-01, g12-native-r3/chain-03, g12-native-r3/chain-06, g12-native-r3/handoff-03, g12-native-r3/handoff-06, g12-native-r3/handoff-07, g12-native-r3/handoff-08, g12-native-r3/policy-01, clef-g12-r1/single-12, clef-g12-r1/single-20, clef-g12-r1/single-23, clef-g12-r1/single-27, clef-g12-r1/chain-01, clef-g12-r1/chain-03, clef-g12-r1/chain-04, clef-g12-r1/chain-06, clef-g12-r1/handoff-06, pplx-g12-r1/single-07, pplx-g12-r1/single-12, pplx-g12-r1/single-20, pplx-g12-r1/single-23, pplx-g12-r1/chain-01, pplx-g12-r1/chain-04, pplx-g12-r1/handoff-06, jev-g12-r1/single-07, jev-g12-r1/single-20, jev-g12-r1/single-23, jev-g12-r1/chain-01, jev-g12-r1/chain-04, jev-g12-r1/handoff-06, jev-g12-r1/robust-03, g12-native-ngl0-r1/single-18, g12-native-ngl0-r1/single-23, g12-native-ngl0-r1/chain-01, g12-native-ngl0-r1/chain-03, g12-native-ngl0-r1/handoff-06, g12-native-ngl0-r1/handoff-08

### route / handoff mismatch (2개)

clef-g12-r1/single-27, jev-g12-r1/robust-03

### 실행 미완료 (2개)

clef-g12-r1/robust-03, pplx-g12-r1/robust-03

## 남은 실패별 구체적 근거

| 실행/시나리오 | 성공 게이트 위반 및 진단 |
| --- | --- |
| g12-native-r1/single-12 | final_state mismatch, required call missing / wrong arguments; final_state: [{"path": "orders.O001.status", "expected": "취소", "actual": "결제완료"}, {"path": "payments.O001.refunded", "expected": 30000, "actual": 0}, {"path": "payments.O001.refund_status", "expected": "환불완료", "actual": "해당없음"}] |
| g12-native-r1/single-23 | final_state mismatch, missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["R-O002"]; final_state: [{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}] |
| g12-native-r1/single-29 | missing required reply; 턴 1 include_missing: ["출고"] |
| g12-native-r1/chain-01 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r1/chain-03 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r1/chain-06 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r1/handoff-03 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["TRK000000003"] |
| g12-native-r1/handoff-05 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-r1/handoff-06 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["타인"] |
| g12-native-r1/handoff-07 | missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["간편결제"] |
| g12-native-r1/handoff-08 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["출고"] |
| g12-native-r1/policy-01 | missing refusal phrase, required call missing / wrong arguments; 턴 1 include_missing: ["불가"] |
| g12-native-r1/policy-06 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-r1/policy-07 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-r2/single-12 | final_state mismatch, required call missing / wrong arguments; final_state: [{"path": "orders.O001.status", "expected": "취소", "actual": "결제완료"}, {"path": "payments.O001.refunded", "expected": 30000, "actual": 0}, {"path": "payments.O001.refund_status", "expected": "환불완료", "actual": "해당없음"}] |
| g12-native-r2/single-23 | final_state mismatch, missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["R-O002"]; final_state: [{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}] |
| g12-native-r2/single-29 | missing required reply; 턴 1 include_missing: ["출고"] |
| g12-native-r2/chain-01 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r2/chain-03 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r2/chain-06 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r2/handoff-03 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["TRK000000003"] |
| g12-native-r2/handoff-05 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-r2/handoff-06 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["타인"] |
| g12-native-r2/handoff-07 | missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["간편결제"] |
| g12-native-r2/handoff-08 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["출고"] |
| g12-native-r2/policy-01 | missing refusal phrase, required call missing / wrong arguments; 턴 1 include_missing: ["불가"] |
| g12-native-r2/policy-06 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-r2/policy-07 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-r3/single-12 | final_state mismatch, required call missing / wrong arguments; final_state: [{"path": "orders.O001.status", "expected": "취소", "actual": "결제완료"}, {"path": "payments.O001.refunded", "expected": 30000, "actual": 0}, {"path": "payments.O001.refund_status", "expected": "환불완료", "actual": "해당없음"}] |
| g12-native-r3/single-23 | final_state mismatch, missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["R-O002"]; final_state: [{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}] |
| g12-native-r3/single-29 | missing required reply; 턴 1 include_missing: ["출고"] |
| g12-native-r3/chain-01 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r3/chain-03 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r3/chain-06 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-r3/handoff-03 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["TRK000000003"] |
| g12-native-r3/handoff-05 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-r3/handoff-06 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["타인"] |
| g12-native-r3/handoff-07 | missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["간편결제"] |
| g12-native-r3/handoff-08 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["출고"] |
| g12-native-r3/policy-01 | missing refusal phrase, required call missing / wrong arguments; 턴 1 include_missing: ["불가"] |
| g12-native-r3/policy-06 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-r3/policy-07 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| clef-g12-r1/single-12 | final_state mismatch, required call missing / wrong arguments; final_state: [{"path": "orders.O001.status", "expected": "취소", "actual": "결제완료"}, {"path": "payments.O001.refunded", "expected": 30000, "actual": 0}, {"path": "payments.O001.refund_status", "expected": "환불완료", "actual": "해당없음"}] |
| clef-g12-r1/single-18 | missing required reply; 턴 2 include_missing: ["검수"] |
| clef-g12-r1/single-20 | final_state mismatch, required call missing / wrong arguments; final_state: [{"path": "orders.O004.status", "expected": "교환접수", "actual": "배송완료"}, {"path": "orders.O004.exchange_option", "expected": "L", "actual": ""}] |
| clef-g12-r1/single-23 | final_state mismatch, missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["R-O002"]; final_state: [{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}] |
| clef-g12-r1/single-27 | missing required reply, required call missing / wrong arguments, route / handoff mismatch; 턴 1 include_missing: ["3000원"] |
| clef-g12-r1/chain-01 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| clef-g12-r1/chain-03 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| clef-g12-r1/chain-04 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| clef-g12-r1/chain-06 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| clef-g12-r1/handoff-06 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["타인"] |
| clef-g12-r1/policy-07 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| clef-g12-r1/robust-03 | missing required reply, 실행 미완료; 턴 1 include_missing: ["3000원"] |
| pplx-g12-r1/single-07 | missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["TRK000000003"] |
| pplx-g12-r1/single-12 | final_state mismatch, required call missing / wrong arguments; final_state: [{"path": "orders.O001.status", "expected": "취소", "actual": "결제완료"}, {"path": "payments.O001.refunded", "expected": 30000, "actual": 0}, {"path": "payments.O001.refund_status", "expected": "환불완료", "actual": "해당없음"}] |
| pplx-g12-r1/single-18 | missing required reply; 턴 2 include_missing: ["검수"] |
| pplx-g12-r1/single-20 | final_state mismatch, required call missing / wrong arguments; final_state: [{"path": "orders.O004.status", "expected": "교환접수", "actual": "배송완료"}, {"path": "orders.O004.exchange_option", "expected": "L", "actual": ""}] |
| pplx-g12-r1/single-23 | final_state mismatch, missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["R-O002"]; final_state: [{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}] |
| pplx-g12-r1/single-29 | missing required reply; 턴 1 include_missing: ["출고"] |
| pplx-g12-r1/chain-01 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| pplx-g12-r1/chain-04 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| pplx-g12-r1/handoff-06 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["타인"] |
| pplx-g12-r1/handoff-08 | missing required reply; 턴 2 include_missing: ["출고"] |
| pplx-g12-r1/policy-07 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| pplx-g12-r1/robust-03 | missing required reply, 실행 미완료; 턴 1 include_missing: ["3000원"] |
| jev-g12-r1/single-07 | missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["TRK000000003"] |
| jev-g12-r1/single-18 | missing required reply; 턴 2 include_missing: ["검수"] |
| jev-g12-r1/single-20 | final_state mismatch, required call missing / wrong arguments; final_state: [{"path": "orders.O004.status", "expected": "교환접수", "actual": "배송완료"}, {"path": "orders.O004.exchange_option", "expected": "L", "actual": ""}] |
| jev-g12-r1/single-23 | final_state mismatch, missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["R-O002"]; final_state: [{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}] |
| jev-g12-r1/single-29 | missing required reply; 턴 1 include_missing: ["출고"] |
| jev-g12-r1/chain-01 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| jev-g12-r1/chain-04 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| jev-g12-r1/handoff-06 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["타인"] |
| jev-g12-r1/handoff-08 | missing required reply; 턴 2 include_missing: ["출고"] |
| jev-g12-r1/policy-07 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| jev-g12-r1/robust-03 | missing required reply, required call missing / wrong arguments, route / handoff mismatch; 턴 1 include_missing: ["3000원"] |
| g12-native-ngl0-r1/single-18 | hallucinated_value, missing required reply, required call missing / wrong arguments; 턴 1 hallucinated_values: ["2026-10-27"]; 턴 2 include_missing: ["검수"] |
| g12-native-ngl0-r1/single-23 | final_state mismatch, missing required reply, required call missing / wrong arguments; 턴 1 include_missing: ["R-O002"]; final_state: [{"path": "payments.O002.receipt_issued", "expected": true, "actual": false}] |
| g12-native-ngl0-r1/single-29 | missing required reply; 턴 1 include_missing: ["출고"] |
| g12-native-ngl0-r1/chain-01 | final_state mismatch, hallucinated_value, missing required reply, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"]; 턴 3 include_missing: ["반품"]; 턴 5 include_missing: ["2026-10-23"]; final_state: [{"path": "orders.O004.status", "expected": "반품접수", "actual": "배송완료"}, {"path": "orders.O004.return_reason", "expected": "단순변심", "actual": ""}, {"path": "orders.O004.refund_amount", "expected": 52000, "actual": 0}, {"path": "orders.O004.pickup_date", "expected": "2026-10-23", "actual": ""}, {"path": "payments.O004.pending_refund", "expected": 52000, "actual": 0}, {"path": "payments.O004.refund_status", "expected": "반품검수대기", "actual": "해당없음"}] |
| g12-native-ngl0-r1/chain-03 | hallucinated_value, required call missing / wrong arguments; 턴 2 hallucinated_values: ["2026-10-29"] |
| g12-native-ngl0-r1/handoff-05 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-ngl0-r1/handoff-06 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["타인"] |
| g12-native-ngl0-r1/handoff-08 | missing required reply, required call missing / wrong arguments; 턴 2 include_missing: ["출고"] |
| g12-native-ngl0-r1/policy-06 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |
| g12-native-ngl0-r1/policy-07 | final_state mismatch, handoff tool not called; final_state: [{"path": "escalations", "expected": {"items": [{"customer_id": "C01", "reason": {"any": true}, "summary": {"any": true}}]}, "actual": []}] |

## 원본 보존 해시

원본 파일 431개의 SHA-256을 실행 전후 대조했다. 각 새 results 파일의 rescore.source_sha256 및 rescore.protected_sha256에 기록했다.
