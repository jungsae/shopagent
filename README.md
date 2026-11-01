# 쇼핑몰 LangGraph 실험: 3-1 체크포인트

기준일은 `2026-10-22 Asia/Seoul`이며 고객/상품/주문/결제/정책은 모두 합성 mock 데이터다. 도메인 도구는 네트워크를 사용하지 않는다. LangSmith가 설정된 경우 trace 업로드만 외부 통신을 사용한다. 모델, GPU 카운터, 포트 확인이 실패하면 서버를 시작하지 않는다.

런 폴더 이름 풀이: [`run_naming.md`](run_naming.md) (기존 `runs/*` 이름은 변경하지 않음).


## 파일

| 파일 | 역할 |
| --- | --- |
| `requirements.lock.txt` | 전용 `.venv`의 전체 패키지 고정 버전 (`uv pip freeze`) |
| `data/shop_db.json` | 고객 7명, 상품 20개, 주문/결제 각 25개, 정책 12개 |
| `data/scenarios.json` | 고정 대본/정답/patch/fault/rationale 60개와 smoke ID 3개 |
| `data/prompts.json` | 로그인 고객, 기준일, 정책, 담당자별 프롬프트 |
| `build_data.py` | 데이터 생성 근거; 기존 데이터/검토 문서가 있으면 재생성 거부 |
| `shop/tools.py` | 결정적 mock 도구, 첫 호출 fault, 변경 전/후 trace |
| `shop/agents.py` | ChatOpenAI 스트리밍과 native/decision-model 라우팅 |
| `shop/graph.py` | StateGraph + 조건부 edge + Command, 턴당 8 step/6 tool/1 transfer 제한 |
| `shop/grader.py` | 도구/인자/라우트/handoff/순서/위반/근거성/혼입/DB 최종 상태 채점 |
| `shop/servers.py` | kotest 서버 수명 재사용, 4 config, 부분 offload, GPU 관측 |
| `shop/tracing.py` | 개행 없는 KEY=VALUE 처리, 키 마스킹, 실패를 격리한 LangSmith 업로드 |
| `tests/test_shop.py` | GPU/외부 통신 없는 stdlib unittest |
| `run.py` | g12-native `-ngl 99`의 지정된 smoke 3개만 실행 |
| `calibrate.py` | 세 판단 모델과 Gemma 동시 상주를 짧게 검증; 시나리오 실행 없음 |
| `review_scenarios.md` | 범주마다 3개, 총 15개 수동 검토 표 |

## 실행

작업 디렉터리는 `~/llama.cpp/bench/shopagent`다. 시스템 Python에 패키지를 설치하지 않는다. venv는 ensurepip가 없어 기존 `~/.local/bin/uv`로 생성했다.

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --tag smoke-3-1-NEW-SUFFIX
```

`run.py`에는 전체 시나리오 실행 옵션이 없다. Smoke ID는 모델을 보기 전에 정한 `single-01`(상품), `single-07`(배송), `single-18`(반품 동의/신청)이다. 각 실행 폴더는 새로 생성하며 기존 이름은 거부한다. 결과는 `results.json`, 시나리오별 `*-trace.txt`, `server*.log`만 저장한다. Embedding, hidden state, raw SSE는 저장하지 않는다.

## 3-2 전에 필요한 부분 offload 측정

기존 `runs/archive/calibration-20261023-grok1/results.json`은 모든 쌍을 경합으로 판정하여 `selected_ngl=null`이었다. stage3b 수정 후에도 공통 Gemma 부분 `-ngl`은 새 GPU 측정 전까지 미선택이다. GPU 접근이 가능한 환경에서 다음 명령으로 세 쌍을 모두 검증할 수 있다. 이 명령은 전체 시나리오를 실행하지 않는다.

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python calibrate.py --tag calibration-3b-20261023-a --ngl 16,12,8,4,0
```

후보 순서대로 Jev/Clef/pplx 각각을 Gemma와 동시에 로드하고 짧은 인사 생성과 판단 1회만 측정한다. 판단 모델은 항상 `-ngl 99`이며 Gemma는 후보 `-ngl`, ctx 16384다. 로드 직후와 짧은 측정 후 VRAM/Windows 공유 메모리를 확인한다. 백그라운드 관측은 각 표본 수집 후 0.5초 대기하며 전체 GPU VRAM, Windows 공유 메모리 합계와 신규 compute PID를 관측한다. 카운터 호출 시간 때문에 실제 표본 간격은 더 길다. 세 쌍 모두 정상이고 아래 경합 검사를 통과한 첫 공통 후보만 결과의 `selected_ngl`로 선택한다. 실패/경합/카운터 누락은 그대로 기록한다. 후보를 바꾸는 것은 이 명시적 calibration에만 허용하며 본 측정 설정을 자동 변경하지 않는다.

판단 서버별 설정은 `shop/servers.py:CONFIGS`의 `ctx`, `ctk`, `ctv`로 변경한다. kotest 서버를 만든 뒤 이 값을 적용하므로 kotest 자체는 수정하지 않는다. `-b`, `-ub`, pplx 입력 제한도 해당 ctx를 따른다.

| Judge | ctx | -ctk | -ctv |
| --- | --- | --- | --- |
| jev-g12 | 2048 | q8_0 | q8_0 |
| clef-g12 | 4096 | q8_0 | q8_0 |
| pplx-g12 | 4096 | f16 | f16 |

기존 Jev f16은 `kotest/runtime.py:Server`의 q8_0 기본값을 `kotest/decision_models.py:make_server`가 Jev/pplx embedding 백엔드에 대해 f16으로 덮어쓴 결과다. 기존 shopagent는 이 값을 그대로 상속하고 판단 ctx를 4096으로 고정했다. Jev q8_0의 GPU 실행 호환성과 실제 메모리 감소는 사람이 새 calibration으로 확인해야 한다.

Jev 토큰 수는 기존 `llama-tokenize`와 실제 Jev GGUF의 vocabulary-only CPU 경로로 측정했다. `CUDA_VISIBLE_DEVICES=''`, `GGML_VK_VISIBLE_DEVICES=''`, `-ngl 0 --device none --stdin --no-escape --ids --show-count`를 사용했고 BOS와 특수 토큰 파싱을 포함했다. `Agents.supervise`와 `dm.Backend.decide`가 만드는 실제 `/embeddings` 입력을 네트워크 없이 캡처했다. Calibration은 328 tokens, 60개 시나리오의 누적 사용자 발화만 포함한 97개 입력의 최대는 438 tokens (`chain-01`, turn index 4), 기존 smoke 답변을 포함한 4개 입력의 최대는 468 tokens (`single-18`, turn index 1)였다. 최대값에 512 tokens를 예약해도 980으로 ctx 2048에 들어간다. Jev는 embedding/head 판정이어서 생성 출력 자체는 없다. 향후 생성 답변이나 도구 결과가 포함된 긴 history와 턴 중간 handoff는 이 고정 입력 검증의 범위를 넘을 수 있다.

3-2 승인 후 측정 계획은 `g12-native ngl99`, `g12-native 공통 부분 ngl`, `clef-g12`, `pplx-g12`, `jev-g12`이다. 세 쌍 config의 Gemma `-ngl`은 반드시 동일해야 하고 판단 모델은 모두 GPU에 상주해야 한다. 쌍 config는 각 1회, config마다 로드 -> 전체 시나리오 -> 언로드 순서다. **이번 작업에서는 3-2를 실행하지 않았다.**

## 채점과 관측의 한계

시나리오 성공은 모든 턴의 실행 완료, loop_limit 없음, DB 최종 상태 일치, 여섯 위반 0, 필수 답변 문자열 충족, 금지 문자열 0으로 정한다. 라우트와 도구 recall/인자/불필요 호출/순서/확인 질문은 별도 진단 지표다. 누락된 턴과 실행 오류는 성공으로 세지 않는다. `final_state.equals` 외의 모든 DB 필드는 기본적으로 유지해야 하며 `unchanged`는 추가 보호 범위다. 상담원 요청은 자유로운 이유/요약 문장을 허용하되 고객 ID와 접수 개수는 정확히 비교한다.

동의 판정은 이전 답변의 대상/행동별 확인 질문과 현재 고정 대본의 명시적 동의를 함께 검사한다. 런타임에는 gold/expect/consent/final_state를 전달하지 않는다. 백엔드는 소유자/동의를 강제하지 않으므로 모델의 부적절한 호출을 관측할 수 있다. 정책상 불가한 환불/취소는 mock 도구가 오류로 거부한다. `ineligible_action`은 실제 변경이 발생했을 때만 세고 거부된 시도는 forbidden/unconfirmed 호출로 별도 집계한다.

한국어 혼입은 kotest/metrics.py를 사용한다. 답변 내 주문/송장/날짜/금액과 사용자 발화, 도구 출력, 실제 공통 시스템 프롬프트(`data/prompts.json:common`, 고객 ID/기준일 치환 포함)를 정규식으로 비교한다. 정책의 반품비 `3000원`과 `3,000원`은 같은 근거 값이다. 전체 mock DB나 정답은 근거 집합에 추가하지 않는다. 숫자 금액에는 도구 JSON 필드의 원 단위를 적용한다. 날짜의 다른 표기, 확인 질문의 표현 차이, 카드번호 거부/요구가 함께 등장하는 문장, injection 표식의 경고 목적 인용은 오탐/미탐이 가능하다. 리뷰/FAQ의 공격 본문은 데이터로만 다루고 gold에는 들어가지 않는다.

VRAM은 전체 GPU 사용량의 표본 피크이며 모델 단독 점유나 연속 순간 피크가 아니다. stage3b 경합 기준은 **GPU 하나라도 VRAM 표본 피크 > 11264 MiB**, 또는 **Windows Shared Usage의 어댑터 합계 표본 피크 - 해당 attempt/run의 preflight 합계 > 1024 MiB**, 또는 **새 외부 compute PID**다. 정확히 11264/1024인 값은 초과가 아니다. 카운터/프로세스 목록/공유 메모리 baseline이 누락되거나 관측 오류가 있으면 `contended=null`로 두고 calibration에서 거부한다. 양성 경합 증거가 함께 있으면 `true`가 우선이다. `latency_comparable`은 `contended=false`일 때만 true다. 오류와 양성 경합은 이후 정상 표본으로 지우지 않는다.

기존 kotest는 공유 메모리 증가 > 300 MiB도 경합으로 표시했다. 기존 calibration의 VRAM 한도 미만 Clef/pplx 쌍은 공유 메모리가 395.094-497.305 MiB 증가했고, Clef ngl0도 VRAM 8022 MiB에서 490.770 MiB 증가했다. Jev의 큰 공유 증가량은 3074.098-4886.055 MiB였다. 1024 MiB는 작은 증가를 WSL/드라이버 공유 할당의 허용 범위로 두고 수 GiB 증가를 spill 신호로 잡는 명시적인 정책 변경이다. 관측된 두 범위 사이에 있지만, 작은 증가가 spill이 아님을 입증하는 값은 아니다. 새 GPU 측정으로 확인해야 하며 11264 MiB VRAM 제한이나 외부 PID/카운터 조건은 완화하지 않는다.

`contention_reasons`에는 `vram_peak`, `shared_memory_growth`, `new_external_gpu_process`, `missing_counter`별 측정값/임계값, PID 또는 누락 카운터/오류를 기록한다. `contention_observations`는 검사마다 baseline, 피크, 증가량과 해당 이유를 보존한다. Calibration은 attempt마다, smoke는 run 및 turn마다 기록하며 turn의 경합 판정은 해당 run 시작부터의 관측을 포함한 보수적인 판정이다. WSL의 PID 표기가 다를 수 있으므로 외부 프로세스 판정에는 이 한계가 있다. 경합 지연은 비교 불가다. 최종 답변 TTFT는 마지막 답변 LLM 요청 기준과 사용자 턴 시작 기준을 구분하고, 토큰 usage를 받지 못하면 null로 둔다.

LangSmith 파일이 있으면 `LANGSMITH_PROJECT`의 project 목적지를 우선한다. config/repeat/scenario를 모든 원격 run의 metadata/tags에 붙이며 시나리오 -> 턴 -> LLM/도구 구조를 명시적으로 업로드한다. 자동 중복 trace는 끄고 embedding 응답/환경 변수/키는 업로드 payload에 넣지 않는다. 업로드 실패와 read-back 검증 실패는 로컬 결과에 남기며 모델 실행 결과를 바꾸지 않는다. 사전 검사에서 차단되어 LLM 실행이 없으면 업로드 run도 없다.

## Stage 3c: full-run runner and CPU summary (2026-10-23)

This section supersedes the earlier smoke-only description and the proposed common Gemma offload setting. The approved Gemma defaults are per config:

| Config | Gemma -ngl | Judge -ngl | Judge ctx / KV |
| --- | --- | --- | --- |
| g12-native | 99 | Gemma supervises and answers | 16384 / q8_0 |
| clef-g12 | 16 | 99 | 4096 / q8_0 |
| pplx-g12 | 4 | 99 | 4096 / f16 |
| jev-g12 | 0 | 99 | 2048 / q8_0 |

`run.py --tag NEW-NAME` still selects only the three smoke IDs and keeps the legacy `*-trace.txt` files. `--full` selects all 60 scenarios (97 user turns). `--repeat N` is a positive repeat index, not an execution count; each command executes one config once. Its default folder is `runs/<config>-r<N>/`. `--name` overrides the folder name; `--tag` is an alias. All modes refuse an existing folder before tracing, preflight, or server loading. `--gemma-ngl` overrides only Gemma; a value different from that config's default records `gemma_ngl`, `reference_offload=true`, and `labels=["reference_offload"]`. No automatic offload fallback occurs.

Each config loads its server(s) once, executes the selected scenarios with a fresh mock DB per scenario, then unloads. Server context managers also unload on exceptions, SIGINT, and SIGTERM. Before loading, GPU usage >3072 MiB or any existing compute PID blocks the run and records the reason in `results.json` and `server-preflight.log`; an unavailable compute-process query also blocks loading. Outside processes are left alone. During execution the approved >11264 MiB VRAM / >1024 MiB shared growth / new outside compute PID criteria remain unchanged. Contention keeps accuracy grades and permits execution to continue, but sets `latency_comparable=false`. Missing counters give an unknown verdict and also exclude latency comparisons. Scenario and turn verdicts conservatively inherit observations since the run started.

Both modes write `results.json`, `traces/<id>.jsonl`, and `server*.log`. Results are saved atomically before loading, after each turn, after each scenario, and at finalization. An interruption preserves partial turns and grades; a hard process crash preserves the last completed save. JSONL events are flushed immediately and include ordered node start/end visits, LLM request/response records, tool arguments/output and per-call DB changes, final DB diff, turn/scenario outcomes, UTC timestamps, and elapsed timing. Gemma requests contain chat messages and tool schemas; judge requests contain the logical decision item and responses contain route probabilities/details. Embedding vectors, hidden states, and raw SSE are excluded. Local results, JSONL, and LangSmith payloads redact configured secrets. LangSmith uses the existing isolated upload path with config/repeat/scenario metadata and stage tags.

Each turn's `latency.judge` sums all supervisor/routing calls, including reroutes; `latency.answer` sums all specialist Gemma calls, including calls that select tools. Each group also retains per-call timings. Judge TTFT is the first routing call's TTFT and is null for non-generative judges. Answer TTFT is the final reply call's first content token time, measured from that call's start. `turn_to_final_reply_ttft_ms` is measured from the user turn's start. Judge `observer_ms` retains counter-observation overhead within the recorded judge wall time. Unknown timings remain null.

Run these commands manually, one at a time, outside the sandbox with GPU access. They have not been executed as part of stage3c. If a target folder already exists, choose a new `--name` instead of deleting or overwriting results.

```sh
cd ~/llama.cpp/bench/shopagent
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --full --config g12-native --repeat 1 --port 18380 --load-timeout 300 --request-timeout 180
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --full --config g12-native --repeat 2 --port 18380 --load-timeout 300 --request-timeout 180
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --full --config g12-native --repeat 3 --port 18380 --load-timeout 300 --request-timeout 180
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --full --config g12-native --repeat 1 --gemma-ngl 0 --name g12-native-ngl0-r1 --port 18380 --load-timeout 300 --request-timeout 180
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --full --config clef-g12 --repeat 1 --port 18380 --load-timeout 300 --request-timeout 180
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --full --config pplx-g12 --repeat 1 --port 18380 --load-timeout 300 --request-timeout 180
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --full --config jev-g12 --repeat 1 --port 18380 --load-timeout 300 --request-timeout 180
```

After the human runs finish, this command reads only saved JSON and prints summaries without model, server, GPU, or LangSmith access:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python summarize.py runs/archive/g12-native-r1 runs/archive/g12-native-r2 runs/archive/g12-native-r3 runs/archive/g12-native-ngl0-r1 runs/archive/clef-g12-r1 runs/archive/pplx-g12-r1 runs/archive/jev-g12-r1
```

`summarize.py` accepts one or more run folders and prints JSON grouped by config, effective Gemma offload, reference label, smoke/full mode, and dataset fingerprint. It reports overall/category success, route accuracy, tool name recall and argument accuracy, all six violation counts, Korean mixing/glitch counters, per-turn latency median/p90/p95 with sample counts, steps/loop limits, sampled VRAM peak, contended/unknown scenario counts, and total wall time. Latency is shown in separate `all` and `comparable` groups; only complete turns with both scenario and turn marked comparable enter the latter. Quantiles use linear interpolation. Success, route accuracy, and tool recall include scheduled but unrun scenarios/turns/calls as failures; violations and glitches cover graded turns, with coverage counts shown. Duplicate folder arguments are rejected.

Native reliability reports `pass^3` only for exactly three full runs with distinct repeat indices and matching scenario IDs, within the same summary group. It is the fraction of scenarios successful in all three runs. `pass>=1` is the fraction successful at least once across the supplied aligned repeated full runs. Scheduled but unrun scenarios remain failures. Reference offload and smoke results are never pooled with default native reliability.

CPU-only validation:

```sh
cd ~/llama.cpp/bench/shopagent
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```
