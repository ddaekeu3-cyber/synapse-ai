# Discord can log in behind proxy, but native command deploy / bot identity fetch fail with 'fetch failed'

## 증상
When Discord is configured behind an HTTP proxy, OpenClaw can partially start the Discord provider and log in successfully, but some startup REST calls still fail with `fetch failed`.

에러 메시지:
```text
[discord] rest proxy enabled
[discord] fetch-application-id:done
[discord] gateway proxy enabled
[discord] deploy-rest:put:error ... error=fetch failed
[discord] failed to deploy native comman

## 원인
원본 이슈에서 확인 필요. GitHub Issue #51018 참조.

## 해결법
succeeds
- application ID fetch succeeds
- the provider still logs in to Discord
- only some REST startup calls fail

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51018
