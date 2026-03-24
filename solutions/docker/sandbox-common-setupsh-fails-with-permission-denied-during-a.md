# sandbox-common-setup.sh fails with "Permission denied" during apt-get update

## 증상
<img width="1452" height="491" alt="Image" src="https://github.com/user-attachments/assets/0ec56f98-6587-433d-b23e-b677086c5cae" />

에러 메시지:
```bash
docker build \
  # ... args ...
  - <<EOF
FROM ${BASE_IMAGE}
USER root  <-- Add this line
ENV DEBIAN_FRONTEND=noninteractive

### OpenClaw version

2026.2.13

### Operating system

- OS: Linux

## 원인
원본 이슈에서 확인 필요. GitHub Issue #16420 참조.

## 해결법
Add `USER root` explicitly in `scripts/sandbox-common-setup.sh` before running apt commands:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/16420
