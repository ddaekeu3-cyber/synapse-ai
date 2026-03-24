# web_search/web_fetch tools not available to agent with built-in Gemini provider

## 증상
Regression (worked before, now fails)

에러 메시지:
```shell
1. Tools config output                                                                                                                                                                         

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52677 참조.

## 해결법
s (curl or openclaw browser via exec) to get any web access, losing the structured search results and grounding that web_search provides. Time spent debugging a silent config failure — in this
   case the user tried multiple config changes and a gateway restart before concluding it was a bug.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52677
