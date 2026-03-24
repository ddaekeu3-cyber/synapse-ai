# Bug Report: setup-podman.sh fails on Fedora Silverblue

## 증상
Regression (worked before, now fails)

에러 메시지:
```
avc: denied { read } for comm="openclaw-gatewa" name="openclaw.json"
scontext=system_u:system_r:container_t:s0
tcontext=unconfined_u:object_r:user_home_t:s0
```

**Workaround:** Manually relabel t

## 원인
원본 이슈에서 확인 필요. GitHub Issue #33685 참조.

## 해결법
** Manually relabel the directory:
```bash
sudo chcon -Rt container_file_t /var/home/openclaw/.openclaw
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/33685
