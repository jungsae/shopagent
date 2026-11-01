# Stage 3b checkpoint - 2026-10-23

Stage 3-1 follow-up fixes are prepared. GPU calibration and smoke commands below are for a human to run outside the sandbox. The common Gemma ngl remains unselected until that calibration succeeds. Stage 3-2 was not run.

## Files changed

| File | Change |
| --- | --- |
| `shop/servers.py` | Per-judge ctx/ctk/ctv settings; local contention evaluator; shared-memory peak sampling; measured trigger reasons; conservative cumulative verdicts. |
| `calibrate.py` | Use the shopagent evaluator before/after short inference and at finalization; reject contention and missing counters; default candidates 16,12,8,4,0. |
| `run.py` | Use the same evaluator for initial residency, each smoke turn, and run finalization. |
| `shop/grader.py` | Include formatted common system prompt policy values in grounding evidence. |
| `tests/test_shop.py` | Judge configuration, policy grounding, threshold boundaries, shared peaks, external processes, missing counters, and cumulative/final verdict tests. |
| `README.md` | Current judge settings, token audit, explicit contention policy and its limits, and calibration command. |
| `checkpoint_3b_20261023.md` | This checkpoint and human-run commands. |

No edits to kotest, existing runs, model files, dependencies, or llama.cpp. No installs, downloads, rebuild, commits, server startup, calibration execution, smoke execution, or nvidia-smi calls. The LangSmith key file was not read or printed.

## Root causes

### Jev inherited f16 KV and a fixed 4096 context

`shop/servers.py:make` called `kotest/decision_models.py:make_server`. `kotest/runtime.py:Server` starts with `-ctk q8_0 -ctv q8_0`, but `decision_models.make_server` explicitly replaces both with f16 for the Jev and pplx embedding backends. Shopagent previously inherited that override and hardcoded ctx 4096 for all three judges. This was an explicit adapter setting, not the llama-server default.

Shopagent now applies each judge's `CONFIGS` values after constructing the reused server. Jev uses `-ctk q8_0 -ctv q8_0 -c 2048`; Clef keeps q8_0/4096; pplx keeps f16/4096. Each judge's `-b`, `-ub`, and the pplx input limit follow its ctx. Gemma stays q8_0/16384. Server records include the effective KV types and command. Actual Jev q8_0 embedding compatibility and memory savings still require the human GPU calibration.

### Low-VRAM rejection was the shared-memory delta check

Read-only source: `kotest/route_answer.py:mark_contention` sets true when current VRAM > 11264 MiB OR the sum of Windows Shared Usage counters grows by > 300 MiB relative to preflight. Missing memory counters give null. It saves boolean checks without trigger details. Shopagent called it immediately after loading the resident pair, before the short warmup/decision calls, despite the snapshot name `memory_after_warmup`.

Read-only evidence: `runs/archive/calibration-20261023-grok1/results.json`, all 12 attempts:

| Pair | Gemma ngl | VRAM peak MiB | Shared growth MiB | Old trigger(s) |
| --- | --- | --- | --- | --- |
| jev-g12 | 12 | 11672 | 127.070 | VRAM |
| clef-g12 | 12 | 10748 | 497.305 | Shared |
| pplx-g12 | 12 | 11762 | 535.648 | VRAM + shared |
| jev-g12 | 8 | 11711 | 4886.055 | VRAM + shared |
| clef-g12 | 8 | 9891 | 432.016 | Shared |
| pplx-g12 | 8 | 11628 | 506.930 | VRAM + shared |
| jev-g12 | 4 | 11680 | 4024.094 | VRAM + shared |
| clef-g12 | 4 | 9054 | 429.586 | Shared |
| pplx-g12 | 4 | 11039 | 400.641 | Shared |
| jev-g12 | 0 | 11682 | 3074.098 | VRAM + shared |
| clef-g12 | 0 | 8022 | 490.770 | Shared |
| pplx-g12 | 0 | 9992 | 395.094 | Shared |

Shared growth above is the recorded after-load total minus that attempt's own preflight total. For Clef ngl0 the exact totals are 47.11328125 -> 537.8828125 MiB, delta 490.76953125 MiB. The baseline was not always about 94 MiB. All six attempts below the VRAM limit tripped the old shared-memory threshold. Existing results are preserved.

The calibration finalizer also used `>= 11264`, while kotest and smoke used `> 11264`. Both shopagent entry points now consistently use `>`. Previously process-monitor errors were recorded without reliably blocking comparable measurements; they now give an unknown verdict and reject calibration. Shared memory is now sampled during the workload as well as at the explicit checkpoints.

### Policy-grounded return fee was omitted from grader evidence

The grader used user text and tool outputs as evidence, but omitted `data/prompts.json:common`, which explicitly supplies the 3000-won change-of-mind return fee. It now includes that actual system prompt with customer/date substitution. Existing normalization equates `3,000원` and `3000원`. Unobserved DB values and gold expectations are not added. Values absent from policy and tools, such as 4321 won, remain flagged.

Read-only CPU regrading of both saved `single-18` turns from `runs/archive/smoke-3-1-20261023-grok1/results.json` produced no hallucinated values and zero of the six violation types. This did not rerun inference or rewrite saved grades; other success criteria still apply.

## Contention criterion and reason records

An attempt/run is `contended=true` if any of these hold:

- Any GPU's sampled VRAM peak > 11264 MiB.
- Peak sampled sum of Windows adapter Shared Usage counters minus the attempt/run's preflight sum > 1024 MiB.
- A compute PID appears that was absent from the process baseline and is not an owned server PID.

Exactly 11264 MiB VRAM or 1024 MiB shared growth is allowed by the memory comparisons. Missing baseline/shared/GPU counters, missing monitor samples, and counter/process-query errors give `contended=null` unless positive contention evidence also exists, in which case true takes precedence. Only false permits `latency_comparable=true` and calibration selection. Positive/unknown observations are retained after later healthy samples. Smoke turn verdicts conservatively inherit run observations.

The shared threshold is an explicit policy relaxation from 300 to 1024 MiB. Low-VRAM Clef/pplx growth was 395.094-497.305 MiB; the large Jev growth was 3074.098-4886.055 MiB. A 1 GiB allowance lies between those groups and treats the smaller group as tolerable WSL/driver shared allocation while retaining a spill signal for multi-GiB growth. These samples do not prove that sub-GiB growth is spill-free. Human GPU validation is still needed. The VRAM limit, external-process condition, and missing-counter rejection remain in force. This threshold alone would not rescue any historical Jev attempt, so it cannot establish a common ngl from the old run.

`contention_reasons` preserves structured reasons named `vram_peak`, `shared_memory_growth`, `new_external_gpu_process`, and `missing_counter`. Memory reasons contain measured values and thresholds; shared reasons also contain baseline and peak; process reasons contain PIDs/names; missing-counter reasons contain counter names and/or errors. `contention_observations` records every check's verdict, measured VRAM peaks, shared baseline/peak/growth, and reasons, alongside the compatibility boolean `contention_checks`. Calibration records these per attempt; smoke records them per run and turn. Shared peaks include background samples collected during loading/inference and the explicit snapshots. Finalization uses saved observations and monitor peaks rather than a new query after unloading.

Sampling remains best effort: the background monitor waits 0.5 seconds after each collection, so actual intervals include counter-query time. It cannot establish continuous instantaneous peaks, and WSL process attribution retains its existing limitations.

## Jev prompt token audit

Used the existing `../build/bin/llama-tokenize` from the bench directory, with `../models/jev-omni/Jev-Omni-Q4_K_M.gguf`. Its source loads only the vocabulary, not model weights. Environment: `CUDA_VISIBLE_DEVICES=''`, `GGML_VK_VISIBLE_DEVICES=''`; flags: `-ngl 0 --device none --stdin --no-escape --ids --show-count`. BOS was enabled and special-token parsing used the tokenizer tool's true default, matching the embedding input. The installed build logged an unavailable CUDA driver during backend initialization; vocabulary-only CPU tokenization completed successfully. No model inference or GPU work was performed.

The audit called `Agents.supervise` with a Jev `dm.Backend` whose client was replaced with a capture stub. It captured the exact `/embeddings` input produced by `dm.Backend.decide`, including numbered options and the Jev turn/thought envelope, and sent that string verbatim to the tokenizer through stdin. No Jev head, server, or network client was loaded. The Jev supervisor decision uses the supervisor instructions and recent messages; it does not use Gemma's full common system prompt or tool-schema prompt.

| Inputs | Count | Longest tokens, including BOS |
| --- | --- | --- |
| Calibration payment-method supervisor question | 1 | 328 |
| All 60 fixtures, accumulating user messages through each turn | 97 | 438 (`chain-01`, turn index 4) |
| Existing smoke histories, including the recorded preceding assistant replies | 4 | 468 (`single-18`, turn index 1) |

The longest audited prompt is 468 tokens. With a 512-token reserve, total budget is 980 tokens, below ctx 2048, leaving 1068 additional tokens. Jev actually returns a head probability distribution from embeddings and does not generate output tokens. No upward ctx adjustment is needed for the audited prompts.

This is a fixed-input audit, not a bound on arbitrary future generated history. The 97 fixture cases omit unknown assistant replies; the four smoke cases include the saved real replies. Long future replies, tool-result histories, and mid-turn handoff inputs can exceed this coverage. Recheck context on the human calibration/full-history runs rather than treating these counts as a guarantee for arbitrary messages.

## CPU validation

From `~/llama.cpp/bench/shopagent`:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

Final result: **46 tests passed**, `Ran 46 tests in 0.610s`, `OK`. Coverage includes positive/negative policy grounding, per-judge overrides, exact memory boundaries, both spill triggers, external processes, missing counters/process queries, peak retention, child snapshots at finalization, and inherited uncertainty. Syntax, trailing-whitespace, and ASCII checks also passed for all five changed Python files. GPU/protocol/memory measurements were not run.

## Exact commands for the human

Run these outside the sandbox with GPU access. Both tags were checked to be unused when preparing this checkpoint; the programs refuse to overwrite existing run directories. The tracing module reads its configured LangSmith env file itself; do not print the key.

```sh
cd ~/llama.cpp/bench/shopagent
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python calibrate.py --ngl 16,12,8,4,0 --tag calibration-3b-20261023-a --port 18480 --load-timeout 300
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python run.py --tag smoke-3b-20261023-a --port 18380 --load-timeout 300 --request-timeout 180
```

Calibration tests only resident pairs and short inference, selecting the first candidate accepted for all three judges. Smoke remains g12-native ngl99 and only `single-01`, `single-07`, and `single-18`. Inspect the new calibration's `selected_ngl`, judge commands, and each attempt's contention reasons, plus the new smoke grades and run/turn comparability. Neither command was executed during this task.
