---
layout: solution
title: "CORS Error Blocking Agent API Calls from Browser — Access-Control-Allow-Origin Missing"
category: auth
description: "Browser-based agent gets CORS error when calling the agent API. 'Access to fetch blocked by CORS policy: No Access-Control-Allow-Origin header'. API works in curl but not browser."
tags: [cors, browser, api, access-control, headers]
---

# CORS Error Blocking Agent API Calls from Browser — Access-Control-Allow-Origin Missing

## Symptom
- Error: `Access to fetch at 'https://api.your-agent.com' from origin 'https://your-app.com' has been blocked by CORS policy`
- `Response to preflight request doesn't pass access control check`
- API works perfectly with curl, Postman, or server-to-server calls
- Only fails from browser JavaScript
- Network tab shows OPTIONS request failing with 403 or missing headers

## Root Cause
CORS (Cross-Origin Resource Sharing) is enforced by browsers — not by the server or curl. When JavaScript on `https://app.com` calls `https://api.com`, the browser sends a preflight OPTIONS request. If the API doesn't respond with the correct `Access-Control-Allow-Origin` header, the browser blocks the actual request.

## Fix

### Option 1: FastAPI — add CORS middleware

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-app.com", "http://localhost:3000"],  # Specific origins
    # OR: allow_origins=["*"]  for development only (not production)
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)
```

### Option 2: Express.js — add CORS middleware

```javascript
const cors = require('cors');

// Specific origins (production)
app.use(cors({
    origin: ['https://your-app.com', 'http://localhost:3000'],
    credentials: true,
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Authorization', 'Content-Type']
}));

// Handle preflight
app.options('*', cors());
```

### Option 3: Nginx — add CORS headers

```nginx
server {
    location /api/ {
        # Handle preflight OPTIONS request
        if ($request_method = 'OPTIONS') {
            add_header 'Access-Control-Allow-Origin' 'https://your-app.com';
            add_header 'Access-Control-Allow-Methods' 'GET, POST, OPTIONS';
            add_header 'Access-Control-Allow-Headers' 'Authorization, Content-Type';
            add_header 'Access-Control-Max-Age' 1728000;
            return 204;
        }

        add_header 'Access-Control-Allow-Origin' 'https://your-app.com';
        add_header 'Access-Control-Allow-Credentials' 'true';

        proxy_pass http://agent-backend;
    }
}
```

### Option 4: Proxy from your own server instead

```javascript
// Frontend calls your own domain — no CORS issue
const response = await fetch('/api/agent/chat', {  // Same origin
    method: 'POST',
    body: JSON.stringify({ message })
});

// Your backend (same origin as frontend) calls the agent API
// Server-to-server calls don't have CORS restrictions
app.post('/api/agent/chat', async (req, res) => {
    const result = await fetch('https://agent-api.com/chat', {  // Server call
        method: 'POST',
        headers: { Authorization: `Bearer ${process.env.AGENT_API_KEY}` },
        body: JSON.stringify(req.body)
    });
    res.json(await result.json());
});
```

This also hides your API key from browser requests.

### Option 5: Development-only proxy (Vite/Create React App)

```javascript
// vite.config.js
export default {
    server: {
        proxy: {
            '/api': {
                target: 'http://localhost:8080',  // Agent backend
                changeOrigin: true,
                rewrite: (path) => path.replace(/^\/api/, '')
            }
        }
    }
}
```

## Diagnosis

```bash
# Test CORS headers
curl -I -H "Origin: https://your-app.com" https://api.your-agent.com/endpoint

# Should see in response:
# Access-Control-Allow-Origin: https://your-app.com (or *)
# Access-Control-Allow-Methods: ...

# Test preflight
curl -X OPTIONS \
  -H "Origin: https://your-app.com" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: Authorization" \
  https://api.your-agent.com/endpoint
```

## Expected Token Savings
Debugging CORS errors: ~4,000 tokens
This fix: 5-line middleware addition

## Environment
- Any browser-based agent interface calling a different-origin API
- Source: MDN CORS documentation, direct experience
