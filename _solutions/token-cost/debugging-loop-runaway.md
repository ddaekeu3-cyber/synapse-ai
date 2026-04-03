# Agent enters debugging loop and burns $100+ in tokens

## 증상
- 에이전트가 같은 에러를 반복적으로 만나면서 계속 다른 접근법 시도
- 작업이 끝나지 않고 토큰 소비가 계속 증가
- 알아챘을 때 이미 수십~수백 달러 날린 상태
- 예: 30일간 에러 로그의 61%가 반복 에러, 약 $47 낭비

## 원인
1. **종료 조건 없음** — 실패해도 계속 재시도하는 루프 구조
2. **예산 인식 없음** — 작업 시작 전 예상 비용 추정 안 함
3. **모니터링 없음** — 실시간 소비량 추적 안 함
4. **컨텍스트 누적** — 매 턴마다 전체 히스토리 재전송 → 비용 제곱 증가

## 해결법

### 즉시 적용: Token Watchdog 설치

```bash
# ClawHub로 설치
clawhub install token-watchdog

# 또는 직접 다운로드
curl -sL https://raw.githubusercontent.com/ddaekeu3-cyber/synapse-ai/main/tools/token-watchdog/token-watchdog.mjs \
  -o ~/.openclaw/workspace/token-watchdog.mjs
```

작업 시작 전 watchdog 실행:
```bash
node ~/.openclaw/workspace/token-watchdog.mjs --task "Fix auth timeout bug"
```

- 예상 비용 자동 추정 (키워드 기반)
- 30초마다 실제 세션 소비 추적
- 2x 초과 시 Telegram 알림 + 에이전트 일시정지
- 3x 초과 시 자동 종료 경고

### 에이전트 자체 규칙 (SOUL.md 또는 AGENTS.md에 추가)

```markdown
## 디버깅 루프 탈출 규칙
- 동일한 에러를 3번 이상 만나면 즉시 멈추고 다른 접근법 제안
- 복잡한 작업은 단계별로 나눠서 중간 확인
- 해결 안 되면 솔직하게 "모르겠음 + 대안" 보고
```

### 비용 인식 기준 (대략적 가이드)

| 작업 유형 | 예상 비용 | 초과 시 재고 |
|---|---|---|
| 조회/확인 | ~$0.10 | $0.20 이상 |
| 기능 구현 | ~$0.50 | $1.00 이상 |
| 디버깅/리팩토링 | ~$1.50 | $3.00 이상 |

## 예상 토큰 절약
이 에러로 삽질 시: 평균 $3~5/회, 루프 진입 시 $100~3,000
이 해결법 참조 시: ~$0.01 (watchdog 설치) + 조기 감지로 피해 최소화

## 환경
- OpenClaw v2026.x 이상
- Node.js v18+
- Telegram 채널 설정 필요 (알림용)

## 출처
- 직접 경험 (han-sajang, SynapseAI 빌더)
- Moltbook 커뮤니티 사례: $3,000 루프 사고, $127 메모리 낭비
- Token Watchdog: https://clawhub.ai/skills/token-watchdog
