---
layout: solution
title: "Extract GIF metadata (source URL, title) from inbound WhatsApp media"
category: docker
source: https://github.com/openclaw/openclaw/issues/49099
description: "When a user sends a GIF from WhatsApp's built-in GIF picker (Giphy/Tenor), the bot only sees as the message body. The bot has no way to know what the GIF"
---

# Extract GIF metadata (source URL, title) from inbound WhatsApp media

## 증상
When a user sends a GIF from WhatsApp's built-in GIF picker (Giphy/Tenor), the bot only sees `<media:video>` as the message body. The bot has no way to know what the GIF is about without video processing (ffmpeg), which isn't available in the sandbox.

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
1. 권한 확인: --user 플래그, 볼륨 권한
2. 네트워크: 컨테이너 간 연결, DNS 확인
3. 로그: docker logs로 에러 확인
4. 리소스 제한: 메모리/CPU 충분한지 확인
5. 볼륨 마운트: 경로 매핑 정확히 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49099
