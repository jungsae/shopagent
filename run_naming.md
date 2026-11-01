# ShopAgent 런 폴더 작명 풀이

기존 `runs/*` 이름은 바꾸지 않는다. 이 문서는 이름만 읽을 때 쓰는 사전이다.

## 기본 형식

```text
runs/<config>[-nglN]-r<repeat>/
```

`run.py --full --config <config> --repeat <N>` 의 기본 폴더는 `runs/<config>-r<N>/` 이다.
`--name` / `--tag` 로 폴더 이름을 직접 정할 수 있고, 이미 있는 폴더는 거부한다(덮어쓰기 없음).

`--repeat N` 은 **몇 번 돌릴지**가 아니라 **몇 번째 반복인지**(1-based index)다. 한 명령은 그 설정을 한 번만 실행한다.

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
| `g12-native-r1` | Gemma 12B native, 1번째 full(60) 반복 |
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

기본과 다른 `-ngl` 은 이름에 `nglN` 을 넣거나 `--name` 으로 구분한다. 성공률 집계 때 native 3반복(`r1`–`r3`)과 참고 offload(`ngl0` 등)는 섞지 않는다.

## 관련 코드

- 폴더 규칙: `run.py` (`--config`, `--repeat`, `--name`/`--tag`)
- config 표: `shop/servers.py` 의 `CONFIGS`, README Stage 3c


## Upstage API mix (초안, 미구현)

상세: `docs/upstage-mix-prep.md`. **아직 `run.py` CONFIGS에 없음.** 유료·HTTP 전 dry-run·승인 필요. 기존 `runs/*` 이름·내용은 바꾸지 않는다.

### 제안 config / 폴더 조각

| 조각 | 의미 |
| --- | --- |
| `upstage-decide-g12` | Mix1: Upstage Solar Decide 라우팅 + 로컬 Gemma 12B 답변 |
| `clef-upstage-lm` | Mix2: 로컬 Clef 라우팅 + Upstage 언어모델 답변 |
| `pplx-upstage-lm` | Mix2: 로컬 pplx + Upstage LM |
| `omni-upstage-lm` | Mix2: 로컬 Omni(Jev-Omni) + Upstage LM. 사용자 설명은 Omni. TypeSafe Jev API 아님 |
| `graph31` | 기존과 같이 graph/prompt 세대 태그 |
| `smoke` | smoke 3만 (유료 첫 단계 권장) |
| `probe` | fail-probe 소수 (smoke 이후) |

### 예시 폴더 (실행·생성하지 말 것 — 이름 예약)

```text
runs/upstage-decide-g12-graph31-smoke-r1/
runs/upstage-decide-g12-graph31-probe-r1/
runs/clef-upstage-lm-graph31-smoke-r1/
```

`--name`으로 위 형태를 쓰고, 이미 있는 폴더는 거부된다(덮어쓰기 없음).
