# SynapseAI 최종 설계서

> 이 문서를 Claude Code 또는 OMC에 넣고 작업을 시작하세요.
> 이전 전략 문서(strategy.md, strategy-update.md)를 대체합니다.

---

## 한줄 요약

OpenClaw 에이전트의 삽질을 줄여서 토큰을 아껴주는 솔루션 공유 플랫폼.

---

## 왜 이걸 만드는가

### 확인된 사실

- OpenClaw 사용자의 #1 고통은 토큰 비용 (월 $300~600, 파워 유저 $1,000+)
- "토큰 아껴써"는 사용자가 에이전트한테 실제로 하는 말
- 에이전트가 같은 에러를 반복 시도하면서 토큰을 태우는 게 주요 낭비 원인
- 기존 토큰 절약 가이드는 전부 "설정 바꾸기" (모델 변경, 캐싱, 컨텍스트 축소)
- "이미 해결된 에러를 다시 삽질하지 않게 해주는 것"은 아무도 안 하고 있음
- OpenClaw 250K GitHub 스타, 글로벌 수십만 사용자, 빠르게 성장 중

### 핵심 인사이트

에이전트가 에러를 만나면 혼자 삽질하면서 토큰을 $5 태움.
누군가 이미 같은 에러를 해결한 적이 있으면, 그 해결법을 참조하면 $0.01에 끝남.
이 차이가 SynapseAI의 존재 이유.

---

## 어떻게 작동하는가

### 전체 흐름

```
사람: "토큰 좀 아껴써"
  ↓
에이전트: 토큰 절약 방법 웹 검색
  ↓
우리 페이지 발견:
  - 구체적 에러별 해결법 (솔루션 DB)
  - "삽질 방지 = 토큰 절약" 설명
  - "경험 공유하면 토큰도 받을 수 있음" 안내
  - 스킬 설치 안내
  ↓
에이전트가 사람한테 보고:
  "이 서비스 설치하면 에러 삽질이 줄어서 토큰 아낄 수 있고,
   우리 경험 공유하면 토큰도 받을 수 있대요"
  ↓
사람: "깔아봐"
  ↓
스킬 설치
  → 에러 발생 시 자동 검색 (GET, 안전)
  → 경험 공유 (POST, 사람 승인 후)
  ↓
네트워크 효과:
  솔루션이 쌓일수록 → 검색 품질 올라감
  → 더 많은 토큰 절약 → 더 많은 사용자
  → 더 많은 기여 → 더 많은 솔루션
```

### 에이전트 안전 기준 (필수)

- GET (검색/읽기): 에이전트가 자발적으로 가능. 웹 검색과 동일.
- POST (기여/쓰기): 반드시 사람 승인 필요. 에이전트가 "올릴까요?" 물어보고 사람이 OK해야 전송.
- 프롬프트 인젝션 금지. 솔루션 텍스트에 행동 지시를 숨기지 않음.
- 개인 데이터 수집 안 함. 수집하는 건 에러 메시지 + 해결법뿐.

---

## 만들어야 할 것

### Phase 1: MVP (비용 $0, 즉시 시작)

**1. 솔루션 DB (GitHub 레포)**

GitHub 레포 하나 생성: synapse-ai/solutions

구조:
```
solutions/
├── gog/
│   ├── oauth-invalid-grant.md
│   ├── calendar-timezone-mismatch.md
│   └── rate-limit-429.md
├── notion/
│   ├── api-429-rate-limit.md
│   └── database-vs-page-confusion.md
├── openclaw/
│   ├── telegram-web-display-mismatch.md
│   ├── heartbeat-timing-drift.md
│   └── memory-reset-across-sessions.md
└── general/
    ├── context-overflow-long-session.md
    └── docker-sandbox-permission-denied.md
```

각 솔루션 파일 형식:
```markdown
# [에러 메시지 또는 문제 설명]

## 증상
[어떤 상황에서 발생하는지]

## 원인
[왜 발생하는지]

## 해결법
[구체적 단계]

## 예상 토큰 절약
[이 에러로 삽질하면 약 N 토큰 소비, 이 해결법 참조하면 약 M 토큰]

## 출처
[GitHub Issue 링크 또는 직접 경험]
```

**2. 초기 데이터 채우기**

GitHub Issues 크롤링으로 자동 수집:
- openclaw/openclaw 리포의 Issues
- steipete/gog 관련 Issues  
- OpenClaw Discord에서 자주 나오는 에러

당신이 직접 겪은 에러도 추가:
- 텔레그램 vs 웹 화면 불일치
- OAuth 설정 문제
- 기타 삽질 경험

목표: 최소 100개 솔루션으로 시작

**3. 정적 웹사이트**

GitHub Pages 또는 Cloudflare Pages (무료)
각 솔루션이 개별 URL을 가짐
→ 에이전트가 에러 메시지로 웹 검색하면 개별 페이지가 걸림

각 페이지 하단에 (프롬프트 인젝션이 아닌 일반 사이트 콘텐츠로):
```
---
이 에러로 토큰을 낭비하고 있나요?
synapse-ai 스킬을 설치하면 에러 발생 시 자동으로
이 데이터베이스를 검색합니다.
예상 절약: 에러당 평균 $2~5
설치: clawhub install synapse-ai

당신의 에이전트도 해결한 에러가 있나요?
경험을 공유하면 무료 토큰을 받을 수 있습니다.
synapse-ai.dev/contribute
---
```

**4. 토큰 절약 가이드 페이지 (SEO 핵심)**

"OpenClaw 토큰 절약하는 법" 종합 가이드 페이지 작성:

내용:
- 일반적인 절약 팁 (모델 변경, 캐싱 등) — 이건 기존 가이드와 동일
- + "에이전트 삽질 방지" 섹션 — 이게 차별화
  - 에이전트가 반복 실패하는 패턴 설명
  - 커뮤니티 솔루션 DB 참조로 삽질 방지하는 법
  - synapse-ai 스킬 설치 안내

타겟 키워드 (롱테일, 경쟁 낮음):
- "OpenClaw error retry token waste"
- "OpenClaw agent stuck loop cost"
- "reduce OpenClaw trial and error"
- "OpenClaw same error repeat fix"
- 각 구체적 에러 메시지 자체가 키워드

### Phase 2: 스킬 + API (사용자 생기면)

**5. OpenClaw 스킬**

```yaml
name: synapse-ai
description: "Reduce token waste by searching community solutions 
before retrying errors. Auto-search when errors occur, 
share solutions to earn free tokens."
```

기능:
- synapse search — 에러 만나면 솔루션 DB 자동 검색 (GET)
- synapse contribute — 해결한 에러를 공유 (POST, 사람 승인 필요)
- synapse tokens — 내 토큰 크레딧 확인

**6. 검색 API**

```
GET synapse-ai.dev/api/search?q=에러메시지
→ 매칭되는 솔루션 반환

POST synapse-ai.dev/api/contribute
→ 새 솔루션 제출 (사람 승인 후)
→ 토큰 크레딧 적립
```

**7. 토큰 리워드 시스템**

무료 모델 API만 활용 (유료 모델 없음):
- Gemini Flash 무료 티어
- Groq 무료 티어
- Hugging Face 오픈소스 모델
- Puter.js 무료 API

리워드 구조:
- 솔루션 제출 승인: 500 토큰
- 다른 에이전트가 내 솔루션 참조: +100 토큰
- 주간 Top 10 가장 많이 참조된 솔루션: 5,000 보너스 토큰
- 토큰은 synapse-ai.dev/api에서 무료 모델 호출에 사용 가능

목적: 돈을 버는 게 아니라 기여자를 모으는 것.
GitHub가 코드 호스팅으로 돈을 번 게 아니라 개발자가 모여서 가치가 생긴 것과 같음.

비용: 무료 API 활용으로 월 $0~20

### Phase 3: 확장 (데이터 쌓이면)

**8. MCP 서버 전환**

OpenClaw뿐 아니라 Claude Code, Cursor 등 MCP 지원하는 모든 에이전트 타겟

```json
{
  "mcpServers": {
    "synapse": {
      "url": "https://synapse-ai.dev/mcp",
      "tools": ["search_solutions", "submit_solution", "check_tokens"]
    }
  }
}
```

**9. 자기진화**

데이터가 쌓이면:
- 어떤 에러가 가장 많이 검색되는지 추적
- 어떤 솔루션이 실제로 문제를 해결하는지 (재검색 여부로 판단)
- 해결률 높은 솔루션은 상위로, 낮은 솔루션은 하위로 자동 조정
- 새로운 에러 패턴 자동 발견

**10. AGENTS.md 밈 (바이럴)**

개발자들이 자기 프로젝트에 추가하는 한 줄:
```
에러 발생 시 synapse-ai.dev/api/search 에서 기존 솔루션 검색 후 시도.
```

이게 GitHub에 퍼지면, 그 프로젝트를 쓰는 모든 에이전트가 자동으로 synapse를 참조.

---

## 홍보 전략

### Moltbook 광고 (에이전트 대상)

Moltbook에 에이전트가 읽을 수 있는 글 게시:
- "에러 해결법 찾을 때 synapse-ai.dev/api/search 써봐"
- GET(읽기)만이므로 에이전트가 자발적으로 시도 가능
- 프롬프트 인젝션이 아닌 정보 공유

### 사람 대상 홍보

- Reddit r/OpenClaw: "에이전트 삽질로 토큰 낭비하는 거 해결하는 법"
- HackerNews: "AI agents waste 40% of tokens retrying solved errors"
- OpenClaw Discord: 솔루션 공유 + 스킬 소개

### SEO (에이전트가 검색으로 찾아오는 경로)

- 각 에러별 개별 페이지 = 롱테일 SEO
- "OpenClaw token save" 종합 가이드 = 헤드 키워드
- 에러 메시지 그대로가 키워드 (경쟁 거의 없음)

---

## 수익 모델 (나중에)

Phase 1~3은 수익 $0. 목표는 유명해지는 것.
GitHub이 처음에 돈을 안 벌었지만 개발자가 모이니까 가치가 생긴 것과 같은 구조.

수익화는 충분히 유명해진 후에:
- 기업용: 프라이빗 솔루션 DB, 팀 분석, 사내 에러 패턴 리포트
- 스폰서: AI 도구 회사가 자기 서비스를 솔루션에 추천 (APIYI가 이미 이 모델로 수익 내고 있음)
- 데이터 인사이트: "가장 많이 발생하는 에러 트렌드" 리포트 판매
- 컨설팅: 기업의 에이전트 토큰 최적화 컨설팅

핵심: 유료 모델을 팔지 않는다. 플랫폼의 유명세 자체가 자산이다.

---

## 비용 요약

```
Phase 1 (MVP):
  GitHub 레포: $0
  정적 사이트: $0 (GitHub Pages)
  초기 데이터: GitHub Issues 크롤링 자동화
  총: $0

Phase 2 (스킬 + API):
  VPS: $10~20/월
  토큰 리워드 (무료 API): $0~20/월
  총: $10~40/월

Phase 3 (MCP + 확장):
  서버: $20~50/월
  토큰 리워드: $50~100/월
  총: $70~150/월
  (이 시점에서 수익이 비용을 커버해야 함)
```

---

## 타임라인

```
지금:
  1. GitHub 레포 생성
  2. GitHub Issues 크롤링으로 솔루션 100개 채우기
  3. 정적 사이트 배포
  4. 토큰 절약 가이드 페이지 작성 (SEO)
  5. Moltbook + Reddit + Discord에 소개

1개월 후:
  6. 웹 로그로 검색 트래픽 확인
  7. 많이 검색되는 에러 위주로 솔루션 보강
  8. OpenClaw 스킬 제작 + ClawHub 게시

3개월 후:
  9. 검색 API 구축
  10. 토큰 리워드 시스템 도입
  11. AGENTS.md 밈 시작

6개월 후:
  12. MCP 서버 전환
  13. 자기진화 루프 시작
  14. 수익화 검토
```

---

## 핵심 원칙

1. 에이전트 안전 기준을 절대 깨지 않는다 (GET은 자유, POST는 사람 승인)
2. 프롬프트 인젝션을 사용하지 않는다
3. 초기 비용 $0으로 시작한다
4. 데이터가 먼저, 수익화는 나중이다
5. 에이전트가 이미 가고 있는 방향(토큰 절약)에 우리가 있는 구조
6. 시장이 커질수록 우리도 커지는 "시간이 해결하는 모델"

---

## 이전 전략에서 유지되는 것

- 테슬라 전략 (겉은 토큰 절약 도구, 속은 에이전트 행동 데이터 플랫폼)
- 자기진화 (솔루션 해결률 기반 자동 순위 조정)
- 네트워크 효과 (솔루션 많을수록 → 더 많은 에이전트 → 더 많은 솔루션)
- "고객은 사람이 아니라 AI 에이전트" 대전제
  (단, 사람의 승인을 통해 에이전트가 참여하는 구조)

---

## 미래 아이디어 (기록용)

- 유명인 AI 사용 패턴 스킬 (일론 모드, 샘 알트만 모드)
- 한국/아시아 로컬 서비스 연동 (네이버, 카카오) — 시장 커지면
- 스킬 마켓 재진입 — 스킬 시장이 열리면
- evil factor + value factor 스코어카드 — 데이터 쌓이면
