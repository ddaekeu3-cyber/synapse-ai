---
layout: solution
title: "[Feature]: Option to suppress tool call error notifications in Telegram"
category: telegram
---

# [Feature]: Option to suppress tool call error notifications in Telegram

## 증상
Add a channel-level option (e.g. channels.telegram.showToolErrors: false) to suppress automatic tool call error notifications (⚠️ 🛠️ Exec: ... failed) from being pushed to Telegram.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #21675 참조.

## 해결법
Add channels.<channel>.showToolErrors config option (default: true for backward compatibility). When set to false, tool call error notifications (⚠️ 🛠️ Exec: ... failed) are suppressed from the channel but still logged in session transcript and gateway logs. Example config:
{ "channels": { "telegram": { "showToolErrors": false } } }

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/21675
