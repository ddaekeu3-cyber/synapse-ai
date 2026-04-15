---
layout: solution
title: "Telegram channel cannot send images via read tool"
category: telegram
source: https://github.com/openclaw/openclaw/issues/48979
description: "When using the tool to read an image file and trying to send it through Telegram channel, the image is not delivered to the user. The read tool"
---

# Telegram channel cannot send images via read tool

## 증상
When using the `read` tool to read an image file and trying to send it through Telegram channel, the image is not delivered to the user. The read tool successfully reads the image (returns image metadata), but the image attachment is not included in the Telegram message.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
Directly calling Telegram Bot API works:

```powershell
$url = "https://api.telegram.org/bot<TOKEN>/sendPhoto"
$form = @{ 
    chat_id = "<CHAT_ID>"
    photo = Get-Item "<IMAGE_PATH>"
    caption = "Description"
}
Invoke-RestMethod -Uri $url -Method Post -Form $form
```

This successfully sends the image.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48979
