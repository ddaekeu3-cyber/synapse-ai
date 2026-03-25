---
layout: solution
title: "clawdock-helpers.sh silently ignores docker-compose.override.yml on clawdock-start"
category: docker
source: https://github.com/openclaw/openclaw/issues/49909
---

# clawdock-helpers.sh silently ignores docker-compose.override.yml on clawdock-start

## 증상
\`clawdock-helpers.sh\` uses explicit \`-f\` flags in \`_clawdock_compose()\`, which disables Docker Compose's auto-merge of \`docker-compose.override.yml\`. The wrapper only includes:

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Add the same conditional check already used for \`docker-compose.extra.yml\`:

\`\`\`bash
if [[ -f "${CLAWDOCK_DIR}/docker-compose.override.yml" ]]; then
  compose_args+=(-f "${CLAWDOCK_DIR}/docker-compose.override.yml")
fi
\`\`\`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49909
