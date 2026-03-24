# app crashes when deploying it in docker

## 증상
It get the following error when starting up the docker setup:

에러 메시지:
```shell
◇
[openclaw] CLI failed: Error: gateway closed (1006 abnormal closure (no close frame)): no close reason
Gateway target: ws://127.0.0.1:18789
Source: local loopback
Config: /home/node/.opencl

## 원인
원본 이슈에서 확인 필요. GitHub Issue #23005 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/23005
