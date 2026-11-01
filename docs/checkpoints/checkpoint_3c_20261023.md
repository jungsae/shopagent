# Stage 3c checkpoint - 2026-10-23

Stage 3-2 runner support and a CPU-only summary are ready. Full GPU runs remain for the human to execute. No model inference, llama-server startup, run.py/calibrate.py server execution, nvidia-smi, installation, download, rebuild, commit, or push was performed. All edits are inside shopagent; kotest and existing shopagent/runs were left untouched. The real LangSmith key file was not read or printed.

## Files changed

| File | Change |
| --- | --- |
| `run.py` | Smoke-compatible CLI plus full config selection, repeat index, named/reference runs, per-config Gemma defaults, one resident load/unload cycle, incremental atomic results, flushed scenario JSONL, progress lines, split latency, scenario contention, partial failure/interruption handling. |
| `summarize.py` | Standard-library-only reader for per-config accuracy/category/route/tool/violation/glitch/latency/VRAM/contention/wall-time statistics and native repeated-run reliability. |
| `shop/servers.py` | Approved Gemma defaults and preflight rejection of existing compute processes or failed process queries. Existing contention thresholds and judge settings retained. |
| `shop/agents.py` | Record logical LLM requests/responses, judge probabilities, and failed-call timings without embedding payloads. |
| `shop/graph.py` | Node visit events and preservation of partial graph state on interruption; tool/handoff event recording. |
| `shop/tools.py` | Tool I/O, DB changes, and elapsed timing through the optional event callback. |
| `shop/tracing.py` | Stage 3-2 tags, LLM request inputs, and secret redaction before remote uploads; isolated failures retained. |
| `tests/test_shop.py` | Mocked runner/lifecycle/trace/latency/preflight/summary tests and tracing/redaction assertions. |
| `README.md` | Appended current settings, output and metric definitions, and exact human-run commands. |
| `checkpoint_3c_20261023.md` | This checkpoint. |

## Behavior and limits

Gemma defaults: g12-native 99, clef-g12 16, pplx-g12 4, jev-g12 0. Paired judges always use 99. Jev retains ctx 2048 and q8_0 KV. An explicit nondefault Gemma value records `gemma_ngl`, `reference_offload=true`, and the `reference_offload` label. The named ngl0 native reference is summarized separately from default native repeats.

`--full --config NAME --repeat N` selects all 60 scenarios and writes `runs/NAME-rN/results.json`, `traces/<id>.jsonl`, and server logs. `--repeat` is an index, not a loop count. `--name` overrides the folder; `--tag` remains its alias. Without `--full`, the original three smoke scenarios and legacy text traces remain available. Existing folders are refused before tracing or preflight.

Preflight blocks loading for GPU usage >3072 MiB or any outside compute PID; it records errors without terminating outside processes. Missing process queries also block. During execution, contention remains VRAM >11264 MiB OR shared growth >1024 MiB OR a new outside compute process. Contended scenarios keep their grades and continue; their latency is incomparable. Unknown counters also exclude latency comparisons. Verdicts conservatively inherit run observations.

Results are atomically saved before loading, after every turn/scenario, and at finalization. JSONL events flush immediately and contain node start/end visits, logical LLM request/response data, tool I/O and DB changes, final DB diff, grade, timestamps, and timing. Native requests contain chat messages and schemas; external judges record decision items and route probabilities/details, excluding embeddings/hidden states/raw SSE. Server context managers handle normal completion, errors, SIGINT, and SIGTERM. A hard kill preserves the latest completed JSON save and flushed trace events but cannot run cleanup handlers.

Per-turn judge total sums supervisor/routing calls, including reroutes; answer total sums specialist Gemma calls, including tool-selection calls. Per-call timings remain available. Answer TTFT is the last reply call's first content token; turn-to-final-reply TTFT is separate. Non-generative judge TTFT is null. Judge observer overhead remains recorded. A run status of complete means execution completed; scenario success is the separate accuracy grade.

The summary prints JSON and separates config/offload/reference/mode/dataset groups. It includes all requested violation and glitch types, argument accuracy, steps/loop limits, and median/p90/p95. Comparable latency includes only complete turns with scenario and turn comparability true. Scheduled unrun scenarios/turns/required calls count against success/route/tool recall; coverage counts distinguish graded data. `pass^3` requires exactly three aligned full runs with distinct repeat indices; `pass>=1` covers supplied aligned repeated full runs. Missing scheduled outcomes count as failures. GPU compatibility and actual timing/accuracy remain unmeasured in this checkpoint.

## Validation

Executed from `~/llama.cpp/bench/shopagent`:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

Final result: **68 tests passed**, `Ran 68 tests in 6.434s`, `OK` (46 existing plus 22 new tests). The mocked full run selected 60 scenarios and 97 turns with one load/unload cycle. Tests cover CLI validation, all offload defaults, reference metadata, existing-folder refusal, incremental partial saves, failure cleanup, interruption and SIGTERM handling, exact preflight memory boundary, outside compute processes, continued contention with retained grades, actual graph JSONL events, failed-stream timings, summary metrics/grouping/reliability, and LangSmith redaction. Test runs use temporary folders, mocked servers/counters, and no live LangSmith service.

Syntax parsing, ASCII, and trailing-whitespace checks passed for all eight changed Python files. No GPU validation was attempted.

## Exact commands for the human

Run one command at a time with GPU access outside the sandbox. These commands have not been executed. Existing target folders are refused; use a new `--name` if needed.

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

CPU-only summary after those runs:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python summarize.py runs/archive/g12-native-r1 runs/archive/g12-native-r2 runs/archive/g12-native-r3 runs/archive/g12-native-ngl0-r1 runs/archive/clef-g12-r1 runs/archive/pplx-g12-r1 runs/archive/jev-g12-r1
```

Optional smoke with a new tag:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --tag smoke-3c-20261023-a --port 18380 --load-timeout 300 --request-timeout 180
```

Stopped after preparing runner support, tests, documentation, and this checkpoint. No stage 3-2 measurement or report was generated.
