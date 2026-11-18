# ShopAgent graph v3.1 후속 작업

선택적 irreversible-툴 확인 게이트는 보류. 올바른 게이트는 명시적 사용자 동의를 요청된 동작·주문·인자에 바인딩하고, 승인 vs 거부·인용 문장·무관한 이전 동의를 구분해야 함. 런타임에 그레이더 시나리오 `consent` 라벨을 재사용하면 평가 정답이 누출됨. 현재 툴 스키마는 이미 확인을 요청하지만, 그래프는 강제하지 않음.

별도 변경으로 `cancel_order`, `request_refund`, `request_exchange`, `change_shipping_address`, `schedule_return_pickup`에 대한 런타임 consent 상태를 정의한 뒤, 실행 게이팅을 켜기 전에 승인·거부·모호·오래된·불일치 확인을 테스트해야 함.

8-step 예산은 3-transfer 캡과 독립; 일반 턴은 세 번째 transfer가 실행되기 전에 step 한도에 도달. step 예산 변경은 별도 검증 필요. Human escalation도 소진된 step/툴 예산에 막힐 수 있음.

Grounding은 규칙 기반 날짜/원 금액 검사이지 의미 검증이 아님. 본 금액이 올바른 주문에 속하는지, pending 툴 호출에 딸린 산문을 검사하는지, 모든 통화/숫자 표기를 커버하지는 않음. 현재 턴 툴 출력은 신뢰 증거이므로, 툴이 메아리친 값은 grounded로 칠 수 있음. 수정은 필수 응답 세부사항을 빼먹거나 질문을 해결하지 않고 폴백할 수 있음. 라이브 모델 정확도·지연은 미측정.

HITL interrupt는 미구현; 결정적 mock escalation이 바로 실행됨. GPU 런이나 라이브 서버 후속 작업은 수행하지 않음.
