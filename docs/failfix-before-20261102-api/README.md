# failfix-before-20261102-api — Mock 백엔드 API 전환 전 스냅샷

날짜: 2026-11-02 (Asia/Seoul).  
기준: 확정 정책 `docs/policy/*.md` (2026-11-02).  
범위: shop/tools.py를 Mock 커머스 백엔드 API로 전환 (공통 오류 형식 {ok,error,message}, 소유자 확인 FORBIDDEN/NOT_FOUND/INTERNAL_ERROR, 반품 자격 사유 코드, 반품비·교환비, RF-03 배송비 회수, cancel_items 부분취소, 시나리오 기준일 수거), build_data.py 데이터 재생성(items, shipping_fee, O008 부분취소), grader/graph/prompts/tests/walkthrough 갱신. GPU·llama-server·유료 API 호출 없음.

`code/` 아래에 원래 상대 경로 그대로 복사했다. 해시는 `SHA256SUMS` 참고.
선행 `failfix-before-*`, `failfix-after-*` 폴더는 삭제하지 않음.
