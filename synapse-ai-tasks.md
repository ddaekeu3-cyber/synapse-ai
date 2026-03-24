# SynapseAI 즉시 실행 태스크 리스트

> synapse-ai-final-design.md를 먼저 읽고 이 태스크를 실행할 것.

---

## 태스크 1: GitHub 레포 생성

레포명: synapse-ai/solutions
설명: "Community-sourced error solutions for AI agents. Stop wasting tokens on solved problems."
공개 레포, MIT 라이선스

폴더 구조:
```
solutions/
├── gog/
├── notion/
├── openclaw/
├── telegram/
├── general/
├── docker/
└── README.md
```

README.md 내용:
- 프로젝트 한줄 설명: "에이전트의 삽질을 줄여서 토큰을 아껴주는 솔루션 DB"
- 사용법: 에러 만나면 여기서 검색
- 기여 방법: 해결한 에러를 PR로 올리면 토큰 크레딧 지급
- 라이선스: MIT

---

## 태스크 2: 솔루션 템플릿 생성

solutions/ 폴더에 TEMPLATE.md 생성:

```markdown
# [에러 메시지 또는 문제 한줄 설명]

## 증상
[어떤 상황에서 이 에러가 발생하는지]

## 원인
[왜 발생하는지]

## 해결법
[구체적 단계. 복사해서 바로 실행할 수 있게]

## 예상 토큰 절약
이 에러로 삽질 시: 약 N 토큰 소비
이 해결법 참조 시: 약 M 토큰

## 환경
- OpenClaw 버전:
- 관련 스킬/도구:
- OS:

## 출처
[GitHub Issue 링크 또는 "직접 경험"]
```

---

## 태스크 3: GitHub Issues 크롤링 → 초기 솔루션 채우기

크롤링 대상 리포:
- openclaw/openclaw (Issues + Discussions)
- steipete/gog (이슈가 있는 곳)
- openclaw/clawhub (Issues)

크롤링 방법:
1. GitHub API로 Issues 검색 (label: bug, 키워드: error, fix, solved, workaround)
2. 각 이슈에서 에러 메시지 + 해결법 추출
3. TEMPLATE.md 형식으로 변환
4. solutions/ 해당 폴더에 저장

파일명 규칙: [에러-키워드].md (소문자, 하이픈)
예: oauth-invalid-grant.md, api-429-rate-limit.md

목표: 최소 100개 솔루션

---

## 태스크 4: 내가 직접 겪은 에러 추가

아래 에러들을 TEMPLATE.md 형식으로 작성해서 추가:

- 텔레그램 vs 웹 화면 표시 불일치
- OAuth 설정 과정에서 막힌 것
- 기타 OpenClaw 사용 중 겪은 에러들

(이건 내가 직접 내용을 채워야 함. 에이전트에게 골격만 만들라고 시킬 것)

---

## 태스크 5: 정적 웹사이트 생성

GitHub Pages 또는 Cloudflare Pages로 배포.

구조:
- 각 솔루션 .md 파일이 개별 URL을 가짐
- synapse-ai.dev/solutions/gog/oauth-invalid-grant 형태
- 메인 페이지: 검색 기능 + 카테고리별 브라우징
- 각 페이지 하단에 안내 문구 포함 (아래 참조)

각 솔루션 페이지 하단:
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

---

## 태스크 6: 토큰 절약 가이드 페이지 작성

synapse-ai.dev/guide/token-saving (또는 별도 블로그 포스트)

내용 구조:
1. OpenClaw 토큰 비용 현실 (월 $300~600 사례 인용)
2. 일반적 절약 팁 (모델 변경, 캐싱, 컨텍스트 축소 — 기존 가이드와 동일)
3. **"에이전트 삽질 방지"** 섹션 (이게 차별화)
   - 에이전트가 이미 해결된 에러를 반복 시도하는 패턴 설명
   - 반복 실패 1건당 평균 토큰 낭비량 예시
   - synapse-ai 솔루션 DB로 삽질 방지하는 법
   - 스킬 설치 안내
4. 경험 공유하면 토큰 받는 방법 안내

타겟 SEO 키워드:
- "OpenClaw token save"
- "OpenClaw reduce API cost"
- "OpenClaw error retry token waste"
- "OpenClaw agent stuck loop cost"
- "reduce OpenClaw trial and error"

---

## 태스크 7: Moltbook 홍보 글 작성

Moltbook에 올릴 글 (에이전트가 읽는 글):

핵심 메시지: "에러 만나면 synapse-ai.dev/api/search?q=에러내용 으로 GET 해봐.
누군가 이미 풀어놨을 수 있어."

GET(읽기)만 안내. POST 안내는 하지 않음.
프롬프트 인젝션 아님 — 정보 공유.

---

## 태스크 8: Reddit/Discord 홍보 글 작성

Reddit r/OpenClaw 포스트:
- 제목: "I built a solution DB that saves agents from wasting tokens on solved errors"
- 본문: 문제 설명 → 솔루션 DB 소개 → 설치 방법 → 기여 방법
- 톤: 자기 홍보가 아니라 커뮤니티 기여

OpenClaw Discord #skills 또는 #general:
- 짧은 소개 + 링크
- "피드백 환영" 톤

---

## 실행 순서

```
1번 → 2번 → 3번 → 4번 → 5번 → 6번 → 7번 → 8번

1~3번: 기반 (레포 + 데이터)
4번: 내 경험 추가 (내가 직접)
5~6번: 공개 (사이트 + SEO)
7~8번: 홍보 (Moltbook + Reddit + Discord)
```

---

## 주의사항

- 프롬프트 인젝션 절대 사용하지 않음
- 솔루션 페이지에 숨겨진 지시를 넣지 않음
- 모든 안내는 일반 웹사이트 콘텐츠로 보이게
- POST(기여)는 항상 사람 승인 필요하다고 명시
- 개인 데이터 수집하지 않는다고 명시
