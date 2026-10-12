# Hummingbot 및 Binance 재검토

재검토일: 2026-09-29

## Hummingbot

검토 기준은 [Hummingbot v2.16.0](https://github.com/hummingbot/hummingbot/releases/tag/v2.16.0)입니다. 이 저장소는 Hummingbot 패키지나 Docker 이미지를 실행하지 않으므로 검토한 태그를 런타임 버전으로 표시하지 않습니다.

[GridStrike 공식 코드](https://github.com/hummingbot/hummingbot/blob/v2.16.0/controllers/generic/grid_strike.py)를 다시 확인했습니다. 예제 기본값은 20배·HEDGE·OKX·WLD-USDT입니다. 이 프로젝트의 설정으로 사용하지 않습니다. Controller가 GridExecutorConfig를 생성하는 확장 지점은 존재하지만 현재 로컬 엔진은 해당 클래스를 호출하지 않습니다.

[GridExecutor](https://github.com/hummingbot/hummingbot/blob/v2.16.0/hummingbot/strategy_v2/executors/grid_executor/grid_executor.py)의 로컬 판단 손절과 거래소에 보존되는 조건부 손절은 별개입니다. Hummingbot을 설치한다고 정전 중 잔여 매수 주문 문제까지 해결되는 것은 아닙니다.

## Binance 수정

이전 문서의 `/fapi/v1/order`에 `STOP_MARKET`을 전송한다는 설명은 현재 조건부 주문 경로와 맞지 않았습니다. [공식 USD-M 거래 API](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order)에 맞춰 아래처럼 수정했습니다.

- 일반 Post-only 주문: `POST /fapi/v1/order`, `type=LIMIT`, `timeInForce=GTX`.
- 보호 손절: `POST /fapi/v1/algoOrder`, `algoType=CONDITIONAL`, `type=STOP_MARKET`, `clientAlgoId`, `triggerPrice`, `workingType=MARK_PRICE`, `closePosition=true`.
- close-all 보호 주문에는 `quantity`와 `reduceOnly`를 함께 보내지 않습니다.
- 보호 활성 상태는 Algo 조회로 확인하고 교체할 때 새 보호 주문을 먼저 확인합니다.
- `priceProtect=false`로 설정했습니다. 괴리율 임계값 때문에 비상 손절 트리거가 지연되는 옵션을 기본 적용하지 않습니다. 이것이 지정 가격 체결이나 슬리피지 제한을 보장하지는 않습니다.
- 일반 주문과 Algo 주문의 조회·취소 경로를 분리하고, Algo의 실제 체결 주문 ID를 로컬 주문 ID에 연결합니다.
- 주문 전 UNKNOWN 의도를 저장합니다. 응답 유실 시 조회를 먼저 하고 조회 실패를 로컬 주문 성공으로 바꾸지 않습니다.
- 체결 수수료·실현 손익과 펀딩 수입을 중복 적용하지 않도록 저장합니다. 담보 통화가 아닌 수수료의 환산은 검증되지 않아 중단합니다.

## 운영상 미확인

위 항목은 공식 스키마 검토와 mock 테스트 결과입니다. 실제 계정의 조건부 주문 접수·취소·체결, 서버 응답의 전체 형태, 테스트넷과 본거래소 차이는 검증되지 않았습니다. 실제 실거래는 차단 상태입니다.

BTCUSDT는 기존 모의거래 기본값을 유지합니다. 이번 점검에서 BTCUSDC 대비 현재 유동성이나 계정별 수수료 우위를 새로 확인한 것은 아닙니다. 둘 중 어느 쪽이 한국 거주 사용자의 계정에서 실제로 이용 가능한지도 미확인입니다.

보호 손절 후 남은 매수 주문이 PC 정전 중 다시 체결될 수 있습니다. 현재 로컬 코드는 복구 후 이를 대조·정리할 수 있지만 장애 중 전체 주문을 원격으로 통제하지 못합니다. 거래소의 자동 취소 기능과 Algo 주문의 상호작용, 전용 외부 감시자, 단절 시 잔여 노출 처리에 대한 실제 검증이 선행되어야 합니다. 이를 해결했다고 주장하는 가짜 플래그는 제공하지 않습니다.
