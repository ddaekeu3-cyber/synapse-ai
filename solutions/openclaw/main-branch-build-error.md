# main branch build error

## 증상
Crash (process/app exits or hangs)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #48035 참조.

## 해결법
ed octal literals and octal escape sequences are deprecated
   ╭─[ ../node_modules/.pnpm/qrcode-terminal@0.12.0/node_modules/qrcode-terminal/lib/main.js:3:13 ]
   │
 3 │     black = "\033[40m  \033[0m",
   │             ─────────┬─────────  
   │                      ╰─────────── 
   │ 
   │ Help: for octal literals use the '0o' prefix instead
───╯

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/48035
