# Sandbox file-tool edits rewrite workspace files to 0600, causing EACCES on host-side file tools

## 증상
Regression (worked before, now fails)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #44077 참조.

## 해결법
manually `chown`/`chmod` affected files after edits. Last known good version is unknown; first clearly observed during testing on 2026.3.8.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/44077
