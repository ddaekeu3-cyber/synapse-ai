# gateway error "error: unknown command '/usr/bin/node<N>' on tumbleweed os

## 증상
The bug occurs when attempting to start the gateway on an opensuse/tumbleweed system.

에러 메시지:
```
> ls -l /usr/bin/node*
-rwxr-xr-x 1 root root    14520 Nov 28 13:43 /usr/bin/node
-rwxr-xr-x 1 root root 56824888 Jan 22 06:53 /usr/bin/node24
```
Note there is no symlinking, but both executables

## 원인
원본 이슈에서 확인 필요. GitHub Issue #27738 참조.

## 해결법
ed src/cli/argv.ts and src/cli/argv.test.ts with Claude's help:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/27738
