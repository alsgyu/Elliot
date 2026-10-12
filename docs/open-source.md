# 퀀트 오픈소스 활용 검토

검토일: 2026-09-29

이전 구현은 Python 표준 라이브러리만 사용했고 Hummingbot 엔진과 퀀트 라이브러리를 재사용하지 않았습니다. 따라서 "충분히 활용했다"고 평가할 수 없었습니다.

| 도구 | 이번 구현에서의 역할 | 상태 |
| --- | --- | --- |
| QuantStats 0.0.86 | 실제 자산 기록·시나리오의 복리 수익률, 최대 낙폭, 조건부 샤프·변동성 | 설치·실행·수치 회귀 검증 |
| pandas 2.3.3 / NumPy 2.2.6 | 시계열·서울 시간대·일별 리샘플링·자본 유출입 보정 | 실제 분석 경로에서 사용 |
| GS Quant | 파생상품·통계·리스크 도구 비교 검토 | 미설치·미사용 |
| Hummingbot v2.16.0 | GridStrike/GridExecutor 공식 소스의 동작·설정 검토 | 런타임 미통합 |
| Lucide 0.468.0 | 일관된 GUI 아이콘 | 로컬 파일로 포함, 오프라인 사용 |

## GS Quant를 기본 의존성으로 넣지 않은 이유

[GS Quant 공식 README](https://github.com/goldmansachs/gs-quant)는 파생상품 분석·리스크와 통계 계산 도구를 설명합니다. 로컬 통계 함수도 있으므로 GS Quant 전체가 기관 계정 필수라고 말하는 것은 부정확합니다. 다만 Goldman Sachs API 접근에는 기관 고객 자격 증명이 필요합니다.

현재 제품은 단일 BTC 그리드의 로컬 성과 분석이 필요하므로 [QuantStats](https://github.com/ranaroussi/quantstats)의 수익률·낙폭·샤프 API를 직접 사용했습니다. 설치 수를 늘리기 위한 중복 통계 라이브러리는 추가하지 않았습니다. GS Quant의 기관 데이터, 자산 가격평가, 시나리오 리스크를 사용한 것처럼 표시하지 않습니다.

`kgridbot/analytics.py`는 사용자의 저장 표본을 pandas Series로 만들고 QuantStats 함수를 호출합니다. 티커 다운로드나 외부 데이터 서비스 호출은 하지 않습니다. yfinance는 QuantStats의 전이 의존성으로 설치되지만 이 프로그램은 다운로드 함수를 호출하지 않습니다. NumPy/통계 계산의 실수 변환은 분석에 한정하고 주문·수수료·위험 금액은 Decimal로 계산합니다.

## 남은 구조적 한계

Hummingbot이라는 커넥터 이름을 설정에 저장하는 것은 Hummingbot 재사용이 아닙니다. 현재 자체 엔진을 Hummingbot 기반이라고 부르면 안 됩니다. Docker CLI는 있지만 이 환경의 Docker daemon은 실행되지 않고, Hummingbot Python 런타임도 설치·검증되지 않았습니다.

원래 목표를 완성하려면 실제 Hummingbot V2의 Controller/Executor 이벤트와 Binance 커넥터를 주문의 단일 실행 주체로 연결하고, 현재 위험 관리·보호 주문·영속 상태를 그 수명주기와 통합해야 합니다. 단순히 GridStrike 설정 파일을 내보내거나 import 한 줄을 추가하는 것으로 충족되지 않습니다. 이번 변경의 검증 범위는 실행 가능한 자체 모의거래 도구와 QuantStats 성과 분석입니다.

라이브 주문, 복잡한 가격평가, 시장 데이터 기반 전략 검증까지 오픈소스 활용을 완료했다고 주장하지 않습니다.
