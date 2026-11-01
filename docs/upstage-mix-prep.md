# Upstage API mix 사전 준비 (로컬 PoC)

날짜: 2026-10-28 (Asia/Seoul)  
범위: **로컬 PoC 준비만**. 이 문서를 쓰는 시점에는 Upstage HTTP를 호출하지 않는다. GPU 벤치·유료 smoke는 별도 승인 후.

관련: 루트 `run_naming.md` (런 폴더 작명), Change B E2E `runs/*-graph31-fixB-r1/`, 유닛 `tests/test_shop.py`.

---

## 결론 (한눈에)

| 상태 | 내용 |
| --- | --- |
| **준비됨** | 로컬 시나리오 60·smoke 3, Upstage mock·dry-run 유닛, 로컬 dual/native config, Change B E2E 기준선, prep·작명·키 위치 설계, Decide body builder·키/HTTP 가드, Mix1 offline CLI, fixB triage 메모 |
| **막힘 (유료 smoke 전)** | Mix1 실행 config·러너 미연결, chat 어댑터(Mix2) 미구현, 실 HTTP 클라이언트 미구현(의도적), `upstage.env` 실키 없음(의도적), 비용 상한 미코드화, TypeSafe Jev API와 분리된 실행 경로 없음 |
| **유료 전 권장** | 아래 체크리스트 무료 항목 완료 → 사용자 승인 → Mix1 smoke 3만. 기존 `runs/*` 덮어쓰기 금지 |

선호 순서: **Mix1** (Solar Decide 라우팅 + 로컬 Gemma 답변) → 통과·예산 확인 후 Mix2.

---

## 범위 / 비범위

- **한다 (무료):** 문서, 시나리오 매트릭스, config·작명 초안, 키 파일 **위치만**, 어댑터·mock **설계**, (추후) 페이로드 fixture 유닛 테스트.
- **하지 않는다 (이 단계):** `api.upstage.ai` 호출, GPU/`llama-server` 기동, 실 API 키 저장, 기존 런 삭제·덮어쓰기, Notion 갱신(별도 요청 없으면).
- **별도 트랙:** TypeSafe **Jev API**(유료 라우팅 서비스). 로컬 Omni 백본 폴더명 `jev-g12`는 **로컬 Omni / Jev-Omni**이며 TypeSafe Jev가 아니다. 사용자 대면 설명에서는 로컬 쪽을 Omni로 부른다.

---

## Mix 정의

### Mix1 (선호) — Solar Decide 라우팅 + 로컬 Gemma 답변

- **라우팅:** Upstage Solar Decide, `POST https://api.upstage.ai/v1/systemone`, model `solar-decide` (공개 문서·베타).
- **답변:** 기존과 같이 로컬 Gemma(`gemma-4-12b-it-Q4_K_M.gguf`) + LangGraph 툴 루프.
- **이유:** VRAM에 decision 모델을 안 올려 경합을 줄이고, 한국어 라우팅만 API로 본다. 로컬 Clef가 쓰던 systemone 계열과 스키마가 맞닿아 어댑터 설계가 단순하다.

초안 config 이름: `upstage-decide-g12`  
초안 런 폴더: `runs/upstage-decide-g12-graph31-smoke-r1/` 등 (`run_naming.md` Upstage 절).

### Mix2 — 로컬 라우팅 + Upstage 언어모델 답변

- **라우팅:** 로컬 `clef` / `pplx` / Omni(`jev-g12` 스택).
- **답변:** Upstage chat(또는 Responses) 호환 LLM — **구체 model id는 미정(unknown)**. 공개 콘솔의 Solar 계열 chat 모델 중 하나를 고르기 전에 dry-run·단가 확인 필요.
- **이유:** 답변·툴콜 품질을 클라우드 LLM으로 비교. GPU에는 decision만(또는 경합 정책에 따라 순차) 올릴 수 있음.

초안 config 이름:
- `clef-upstage-lm`
- `pplx-upstage-lm`
- `omni-upstage-lm` (코드 키는 당분간 `jev-g12` 계열과 맞출 수 있으나 **문서·사용자 설명은 Omni**)

---

## TypeSafe Jev API와의 분리

| | Upstage Mix1 | TypeSafe Jev API | 로컬 Omni (`jev-g12`) |
| --- | --- | --- | --- |
| 역할 | Solar Decide 라우팅 | Jev 서비스 라우팅 | 로컬 GGUF+decision-head |
| 엔드포인트 | `api.upstage.ai` `/v1/systemone` | TypeSafe 측 (이 prep 범위 밖) | `127.0.0.1` llama-server |
| 이 문서 | 대상 | **비대상** — 별도 승인·키·러너 | 이미 로컬 E2E 있음 |

세 경로는 결과 폴더·config·키 파일을 섞지 않는다.

---

## 시나리오 매트릭스

기준 데이터: `data/scenarios.json` (60 / 턴 97).  
기준 실패: Change B fixB 런 `runs/{clef,pplx,jev,g12-native}-g12-graph31-fixB-r1` (jev 폴더명 = 로컬 Omni).

**민감도**
- **routing-sensitive:** 첫 담당 선택·핸드오프 경로가 점수에 크게 영향.
- **reply-sensitive:** 도구 호출·확인 질문·필수 문구·final_state·토큰 한도 등 답변 LLM/그래프 쪽이 큼.
- 한 ID에 둘 다 있으면 표에 둘 다 표시.

| ID | 역할 | 민감도 | 유료 단계 | 비고 (fixB에서 본 현상, 요약) |
| --- | --- | --- | --- | --- |
| single-01 | smoke | 균형 | **smoke** | 통과 기준선 |
| single-07 | smoke | 균형 | **smoke** | 통과 기준선 |
| single-18 | smoke | 균형 | **smoke** | 통과 기준선 |
| single-16 | fail-probe | reply (+부분 route) | probe | 조회만·`불가` 문구·툴 recall |
| single-29 | fail-probe | reply (+clef는 route) | probe | 정책 답 `출고` 누락 |
| single-19 | fail-probe | reply | probe | 할루시네이션 값 (dual 일부) |
| single-20 | fail-probe | reply | probe | 교환 확인·final_state |
| handoff-08 | fail-probe | routing + reply | probe | 핸드오프 후 `출고` 문구 |
| policy-01 | fail-probe | routing + reply | probe | 예외 환불 거절·툴/`불가` |
| robust-03 | fail-probe | reply | probe | 출력 토큰 한도 오류 (로컬) |
| chain-01 | fail-probe | reply | probe | native만 체인 붕괴; dual은 대체로 OK |

**유료 경로 (합의)**  
1) Mix1 **smoke 3만** → 2) 예산·결과 보고 후 fail-probe **소수**만 → 3) full 60은 별도 승인.  
로컬에서 이미 답변/문구로 깨지는 probe는 Upstage 호출 전에 원인을 적어 두고, Mix1(라우팅만 API)에서는 “같은 답변 실패가 재현되는지”만 본다.

---

## 비용·호출 추정 / 중단 조건

### 공개 단가 (문서 기준, 변동 가능)

출처: Upstage Console / System One·Solar Decide 공개 페이지 (2026-10 조사). **청구서·콘솔 견적이 최종.**

| 항목 | 공개로 알려진 값 | 비고 |
| --- | --- | --- |
| Solar Decide (`solar-decide`) | 입력 약 **$0.05 / 1M tokens**, **출력 토큰 과금 없음**(프로즈 미생성) | 베타; `/v1/systemone` 스키마 변경 가능 고지 |
| Solar Decide Flash 등 변형 | OpenRouter 등 제3자 목록에 더 낮은 입력가가 보임 | **Upstage 직결 청구와 다를 수 있음 → unknown으로 재확인** |
| Mix2 답변용 chat LLM | **unknown** — model id·단가 미확정 | 콘솔에서 model·$/1M in/out 확정 전 호출 금지 |

캐시 토큰·할인은 콘솔 표기를 따른다. 이 문서에 없는 숫자는 **추정하지 않는다.**

### 호출 수 (대략, Mix1)

- 라우팅: 사용자 턴마다 supervisor Decide **≥1** (재라우트 시 추가).
- 답변: 로컬 Gemma만 → **Upstage 과금 없음**.
- smoke 3: 시나리오당 턴 수는 `scenarios.json` 기준 (대개 1턴 위주). fail-probe·체인은 턴이 많아 Decide 호출이 는다.
- Mix2: 답변 LLM이 툴 루프마다 chat 호출 → **과금 대부분 여기**. 단가 unknown이라 smoke 전에 콘솔에서 확정.

### Hard-cap / abort (실행 시 코드·운영에 넣을 초안)

1. `UPSTAGE_API_KEY` 없으면 원격 호출 **거부** (예외로 끝, 부분 런 폴더는 기존 규칙대로 보존).
2. dry-run/mock 모드가 아니면… 아니, **dry-run 통과 + 사용자 승인 전 HTTP 금지**.
3. 런 시작 시 `max_upstage_calls` (예: smoke 30, probe 추가 N) — 초과 시 즉시 중단.
4. 선택: 일일/런당 USD 상한은 콘솔 알림 또는 수동; 코드에 금액을 박으려면 단가 확정 후.
5. HTTP 4xx/5xx·타임아웃 연속 시 중단; 기존 폴더 덮어쓰기 거부 유지.
6. 끝나면 `llama-server` 0 확인.

---

## 키 위치 설계 (실키 넣지 말 것)

- 경로: `~/.config/shopagent/upstage.env` (LangSmith와 같은 디렉터리).
- 예시 템플릿(저장소): `docs/upstage.env.example` — 플레이스홀더만.
- 권장 키 이름(초안):
  - `UPSTAGE_API_KEY=`
  - `UPSTAGE_BASE_URL=https://api.upstage.ai` (기본)
  - `UPSTAGE_DECIDE_MODEL=solar-decide`
  - `UPSTAGE_CHAT_MODEL=` (Mix2, 확정 전 비움)
- 파일 모드 `600`. git·런 trace·LangSmith 페이로드에 키 금지(기존 redaction 확장).
- **이 prep 단계에서 실키 파일 생성·채우기 금지.**

---

## 어댑터 설계 스펙 (구현 전)

**공통 가드:** 실 HTTP는 (1) mock/dry-run 테스트 통과 (2) 사용자 명시 승인 (3) 키 파일 존재 후에만.

### Mix1 — Decide

현재 로컬 dual은 `Agents.supervise`가 `decision_item`을 만들고 `backend.decide(item)` 호출 (`shop/agents.py`). Clef는 `Client` → `/v1/systemone`.

Upstage Solar Decide 공개 요청 형태(요지):

- URL: `{UPSTAGE_BASE_URL}/v1/systemone`
- Header: `Authorization: Bearer …`
- Body: `model`, `state`, `questions` (choice / score / noul 등)

매핑 초안:

| shopagent | Upstage |
| --- | --- |
| item 대화 텍스트(+instructions) | `state` (string 또는 JSON) |
| `questions.route` choice + options | `questions.route` type `choice` + criteria/options |
| 응답 확률 분포 | `answers.route.probabilities` (필드명은 구현 시 공개 스키마로 재확인) |
| `dm.valid_probs` 후 argmax | 동일 검증 후 route 이름 |

로컬 Clef body와 필드명이 다르면 **변환 레이어**만 두고 Backend kind `upstage`를 추가하는 쪽이 안전하다. 스키마는 베타라 fixture로 고정 스냅샷을 둔다.

### Mix2 — Chat 답변

- `LocalLLM`이 지금 `http://127.0.0.1:{port}/v1` + `api_key=local`.
- Mix2: `base_url`을 Upstage OpenAI 호환 chat 엔드포인트로, `model`=`UPSTAGE_CHAT_MODEL`, 키는 env.
- 툴콜·streaming·`max_tokens` 계약은 유닛의 MockTransport 패턴(`tests/test_shop.py`)을 원격용 fixture로 확장.
- model id 미정이면 config를 머지하지 않는다.

### Mock / dry-run 준비 (순수 helper 구현 완료)

목표: **네트워크 없이** 요청 JSON 모양만 검증하고, 키 없으면 원격 거부.

1. `tests/fixtures/upstage/`: `decide_item.json`, `decide_request.json`. 응답·chat fixture는 미구현.
2. `shop/upstage.py`의 순수 함수 `build_decide_body(item) -> dict` 스냅샷 비교.
3. `post_systemone`: 키 없음·빈 값이면 먼저 거부. 키와 `SHOPAGENT_UPSTAGE_ALLOW_HTTP=1`이 있어도 HTTP 미구현으로 거부. 키 파일은 읽지 않는다.
4. `tests/test_upstage.py`로 검증하고 기존 테스트 회귀 유지; GPU·유료 없음.

5. `python -m shop.upstage_dryrun`: 기본 smoke_ids의 각 턴에 대해 `scenario_id`, `turn_index`, `item`, `body` JSON 출력. `--ids single-01,single-07`로 선택하거나 `--out /tmp/upstage-decide.json`으로 새 파일에 저장한다. 기존 파일과 `runs/` 경로는 거부한다.
6. 첫 턴은 `human: {user}`로 실제 첫 routing item과 일치한다. 후속 턴은 최근 user 발화 최대 10개만 누적하며, 알 수 없는 assistant/tool 답변은 포함하지 않는다. 실제 다중 턴 상태나 정확도를 재현하는 테스트는 아니다.

현재는 순수 helper·fixture·offline CLI·`unittest`만 구현. CLI는 `post_systemone`을 호출하지 않으며 키·ALLOW 설정과 무관하게 JSON만 만든다. `upstage-decide-g12`는 `shop/servers.py`의 주석으로만 남긴다. `run.py --config` 실행 config·HTTP·GPU는 범위 밖.

---

## 체크리스트

### 무료 (완료 또는 진행)

- [x] 로컬 시나리오·유닛·dual E2E 기준선 존재 확인
- [x] 이 prep 문서 (`docs/upstage-mix-prep.md`)
- [x] 런 작명 Upstage 절 (`run_naming.md`)
- [x] 키 위치·env 예시 (`docs/upstage.env.example`)
- [x] 어댑터·mock 설계 기록
- [x] mock 페이로드 fixture + 유닛 (`shop/upstage.py`, `tests/test_upstage.py`): 키 없음·빈 값이면 원격 거부, 키와 `SHOPAGENT_UPSTAGE_ALLOW_HTTP=1`이 있어도 HTTP 미구현으로 거부
- [x] Mix1 dry-run wiring: `python -m shop.upstage_dryrun`, smoke_ids → supervisor item → Decide body, HTTP·서버 호출 없음 (`upstage-decide-g12`는 주석만 유지)
- [ ] Mix2는 chat model·단가 확정 후
- [x] fixB 잔여 실패 로컬 트리아지 메모(문구/토큰/거절) — [upstage-probe-triage.md](failfix-after-20261028-B/upstage-probe-triage.md), 유료 probe 축소
- [x] Change C 로컬 수정과 유닛 121개 통과: 토큰/ID 경계/P05/조회·거절/교환 확인 ([CHANGELOG](failfix-after-20261028-C/CHANGELOG.md)). GPU E2E 미실행, 사용자 요청 전 rebench 금지
- [ ] 사용자: 키를 vault/`upstage.env`에 넣을지 여부만 결정 (채우기는 유료 직전)

### 유료 (명시 승인 후)

- [ ] Mix1 smoke 3, 새 `--name`, cap 적용
- [ ] 결과 보고 후 fail-probe 소수
- [ ] Mix2는 별도 승인·단가 확정 후
- [ ] full 60·반복은 그 다음
- [ ] 종료 시 `llama-server` 0

---

## 다음 무료 한 일

1. 완료: Mix1 offline dry-run wiring과 fixB 잔여 8개 triage 메모.  
2. 완료: Change C 로컬 수정과 유닛 검증. 다음 GPU 검증은 사용자 요청 후이며, 이후 유료 probe 범위를 좁힌다.  
3. **HTTP·GPU·실키는 사용자 승인 전 금지.**
