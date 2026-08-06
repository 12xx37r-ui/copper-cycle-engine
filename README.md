# Copper Cycle Engine

무료 데이터만 사용해 구리 전용 JSON을 생성하는 GitHub Actions 엔진입니다.

## 설치
1. 이 폴더 전체를 새 공개 GitHub 저장소 `copper-cycle-engine`에 업로드합니다.
2. Actions 탭에서 **Update Copper Fundamentals**를 한 번 수동 실행합니다.
3. 생성 파일: `public/data/copper_fundamentals.json`
4. GAS v48은 기본적으로 `12xx37r-ui/copper-cycle-engine`에서 위 JSON을 읽습니다.

## 원칙
- CME 재고 직접 수집은 사용하지 않습니다.
- 무료 공식 데이터가 기계 판독 불가능하면 값을 만들지 않습니다.
- 양산항 프리미엄과 TC/RC는 이름을 그대로 사칭하지 않고 무료 프록시로 표시합니다.
- GitHub Actions가 실패해도 이전 정상 JSON은 유지됩니다.

## 수집 항목
- HG=F 가격·장기 백분위·1년 위치·일/주봉 캔들
- 개별 COMEX 계약을 이용한 선물곡선
- CFTC COT
- LME/SHFE 공식 페이지 재고 수집 시도와 13주 이력 계산
- 중국 실물수요 프록시(FXI+구리 모멘텀)
- 정광 수급 프록시(COPX/구리 상대강도)
- 광산 공급차질 뉴스 RSS 점수

실제 양산항 프리미엄 및 TC/RC의 안정적인 무료 공식 API는 없으므로 프록시임을 JSON과 UI에 명시합니다.
