---
layout: solution
title: "Control UI context-notice SVG icon overflows and covers entire chat window"
category: docker
source: https://github.com/openclaw/openclaw/issues/47924
---

# Control UI context-notice SVG icon overflows and covers entire chat window

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #47924에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
**Option 1: Add width/height attributes to SVG:**
```html
<svg class="context-notice__icon" width="24" height="24" viewBox="0 0 24 24" ...>
```

**Option 2: Add CSS (preferred):**
```css
.context-notice__icon {
  width: 24px;
  height: 24px;
  flex-shrink: 0;
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47924
