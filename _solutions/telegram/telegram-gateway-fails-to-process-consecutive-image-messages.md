---
layout: solution
title: "Telegram gateway fails to process consecutive image messages"
category: telegram
---

# Telegram gateway fails to process consecutive image messages

## 증상
**Fecha:** 2026-02-21

에러 메시지:
`) para obtener más detalles, pero la ejecución falló con un error de `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #23089 참조.

## 해결법
)

Indicar al usuario que envíe un mensaje de texto corto entre el envío de archivos multimedia (imágenes, audios, etc.).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/23089
