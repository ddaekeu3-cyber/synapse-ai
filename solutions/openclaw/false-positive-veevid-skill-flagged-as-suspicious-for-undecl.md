# False positive: veevid skill flagged as suspicious for undeclared config path

## 증상
[meigesir/veevid](https://clawhub.ai/meigesir/veevid) — Veevid AI Video Generator



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1077 참조.

## 해결법
Either:
1. **Adjust the scan heuristic** to distinguish between skills that reference config files for legitimate API key storage vs. actually suspicious file access patterns.
2. **Allow skill authors to declare config paths** in the manifest so the scanner can validate consistency without flagging.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1077
