# ShopAgent graph v3.1 체크포인트

CPU 전용 로컬 변경; 라이브 추론 또는 GPU 재측정 없음.

변경된 파일:
- `shop/graph.py`: 결정적 `human` → `escalate` 노드가 `Shop.invoke("escalate_to_human", ...)`를 호출. 최신 사용자 메시지를 reason/summary에 쓰고, 툴 교환을 기록하며, 고정 확인 또는 실패 응답을 반환. Native와 decision 라우팅 모두 이 경로를 사용. 전문 LLM이나 HITL interrupt는 필요 없음.
- `shop/graph.py`: `MAX_TRANSFER_BACK_PER_TURN = 3`으로 transfer 캡 설정. Transfer는 여전히 단독·무인자여야 하며, 거부 메시지에 설정된 캡이 포함됨.
- `shop/graph.py`: 최종 응답은 기존 그레이더의 ISO/한국어 날짜 및 원(won) 금액 정규화를 사용. 증거는 제공된 히스토리의 사용자 메시지와 현재 턴의 도메인 툴 출력. 숫자 금액 툴 필드 포함. Assistant 초안, 툴 인자, 시스템 정책, 이전 턴 툴 출력만으로는 값을 grounding하지 않음.
- `shop/graph.py`: 지원되지 않는 값은 시스템 노트와 함께 최대 한 번의 수정 호출을 유발. 거부된 초안은 `visible`에 남지 않으며, raw LLM 트레이스에는 유지. 수정 실패, 수정 중 툴 호출, 또는 남은 step 예산 없음이면 숫자 없는 고정 응답과 `grounding_failed=True`. 수정은 툴을 실행할 수 없음. `grounding_retries`와 `ungrounded_values`가 현재 결과를 노출. 고정 응답의 final-reply TTFT는 null.
- `shop/tools.py`: escalate 목록 append가 툴 diff에 보이고 이후 append가 이전 트레이스 기록을 바꾸지 못하도록, 평탄화된 before/after DB 스냅샷을 복사.
- `tests/test_shop.py`: native/decision human 라우트, 재라우팅, escalation 장애와 트레이스 스냅샷, transfer 캡/설정/검증, 날짜·금액 grounding, 증거 경계, 단일 재시도, mutation 방지, 예산 소진을 커버. Mock된 handoff-05/policy-06/policy-07이 기존 시나리오 그레이더를 통과.

검증:
- Baseline: unittest 82개 통과.
- Final: `shopagent/`에서 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q`로 unittest 92개 통과.
- 기존 한도는 여전히 8 steps, 6 tool calls. step 예산이 세 번째 transfer 전에 턴을 멈출 수 있음; 캡 경계는 tools 노드에서 직접 테스트. Human escalation도 이 예산을 따름.
- `parallel_tool_calls=False`는 변경 없음. 의존성 설치, 서버 기동, 모델 다운로드, 커밋 없음.

후속: `graph_v31_followup.md` 참고. 이 체크포인트는 라이브 모델 pass rate 개선을 주장하지 않음.
