# Podman EACCES: permission denied, open '/home/node/.openclaw/openclaw.json'

## 증상
After deploying version 2026.2.25 using ./setup-podman.sh container statup fails

에러 메시지:
```shell
said@fedora44:/tmp$ sudo -u openclaw podman logs openclaw                                                                       
Failed to read config at /home/node/.openclaw/openclaw.json Er

## 원인
원본 이슈에서 확인 필요. GitHub Issue #27336 참조.

## 해결법
ownership with:                                                                                      
  chown 1001 "/home/node/.openclaw/openclaw.json"
Then restart the gateway.                        
Config invalid           
File: ~/.openclaw/openclaw.json
Problem:                  
  - <root>: read failed: Error: EACCES: permission denied, open '/home/node/.openclaw/openclaw.json'
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/27336
