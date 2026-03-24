# openclaw-gateway crashes with SIGILL (invalid opcode) in libvips-cpp during webchat image-flow repro

## 증상
Crash (process/app exits or hangs)

에러 메시지:
```shell
## Relevant logs
### Kernel
`Mar 06 19:11:33 ... kernel: traps: openclaw-gatewa[...] trap invalid opcode ... in libvips-cpp.so.8.17.3[...]`

### systemd (user unit)
`Mar 06 19:11:34 ... openc

## 원인
원본 이슈에서 확인 필요. GitHub Issue #38260 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/38260
