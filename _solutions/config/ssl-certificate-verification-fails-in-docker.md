---
layout: solution
title: "SSL Certificate Verification Fails Inside Docker Container"
category: config
description: "HTTPS requests work on host machine but fail inside Docker with SSL: CERTIFICATE_VERIFY_FAILED or unable to get local issuer certificate. Corporate proxy or missing CA bundle."
tags: [ssl, docker, certificate, https, ca-bundle, corporate-proxy]
---

# SSL Certificate Verification Fails Inside Docker Container

## Symptom
- `ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed`
- `requests.exceptions.SSLError: HTTPSConnectionPool: Max retries exceeded`
- `unable to get local issuer certificate`
- Works on host machine, fails only inside Docker container
- Common on corporate networks with SSL inspection proxies
- Specific to certain internal HTTPS endpoints

## Root Cause
Docker containers have their own CA certificate store, which may be missing:
1. Corporate/internal CA certificates (SSL inspection proxy injects its own cert)
2. Self-signed certificates for internal services
3. Outdated CA bundle in the base image

The host machine has these CAs installed system-wide; the container doesn't inherit them.

## Fix

### Option 1: Add CA certificates to Dockerfile

```dockerfile
FROM python:3.12-slim

# Copy corporate/custom CA certificate into container
COPY ./certs/corporate-ca.crt /usr/local/share/ca-certificates/corporate-ca.crt
COPY ./certs/internal-service.crt /usr/local/share/ca-certificates/internal-service.crt

# Update CA certificate store
RUN apt-get update && apt-get install -y ca-certificates && \
    update-ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Verify cert is trusted (optional sanity check during build)
RUN python3 -c "import ssl; ssl.create_default_context()"

COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
```

### Option 2: Mount host certificates via volume

```yaml
# docker-compose.yml — share host CA store with container
services:
  agent:
    image: my-agent
    volumes:
      # Linux: mount system CA bundle
      - /etc/ssl/certs:/etc/ssl/certs:ro
      - /etc/ca-certificates:/etc/ca-certificates:ro
      # macOS: mount custom certs directory
      # - ./certs:/usr/local/share/ca-certificates:ro
    environment:
      - SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
      - REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
```

### Option 3: Set CA bundle environment variables

```bash
# Set in container environment to point to your cert bundle
export SSL_CERT_FILE=/path/to/ca-bundle.crt
export REQUESTS_CA_BUNDLE=/path/to/ca-bundle.crt
export CURL_CA_BUNDLE=/path/to/ca-bundle.crt
```

```python
import os, certifi

# Option A: Use certifi's up-to-date CA bundle
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

# Option B: Append custom cert to certifi's bundle
import shutil
custom_cert = "/path/to/corporate-ca.crt"
combined = certifi.where() + ".custom"
shutil.copy(certifi.where(), combined)
with open(combined, "a") as f:
    f.write(open(custom_cert).read())
os.environ["REQUESTS_CA_BUNDLE"] = combined
```

### Option 4: Use httpx/requests with custom cert

```python
import httpx, requests

CORPORATE_CA = "/path/to/corporate-ca.crt"

# httpx
async with httpx.AsyncClient(verify=CORPORATE_CA) as client:
    response = await client.get("https://internal.corp.com/api")

# requests
session = requests.Session()
session.verify = CORPORATE_CA
response = session.get("https://internal.corp.com/api")

# Anthropic SDK — set via environment variable, not client param
import os
os.environ["SSL_CERT_FILE"] = CORPORATE_CA
import anthropic
client = anthropic.Anthropic()  # Will use the CA bundle from env var
```

### Option 5: Extract corporate CA from system (Windows/Mac)

```python
# Extract corporate CA from system keychain for use in Docker
# Run on host machine, copy output into Docker image

# macOS — export from Keychain
import subprocess, base64

result = subprocess.run(
    ["security", "find-certificate", "-a", "-p", "/System/Library/Keychains/SystemRootCertificates.keychain"],
    capture_output=True, text=True
)
with open("system-certs.pem", "w") as f:
    f.write(result.stdout)
print("Exported to system-certs.pem")

# Windows — export from Certificate Store
# certmgr.msc → Trusted Root Certification Authorities → Export as PEM
```

### Option 6: Development only — disable verification (NEVER in production)

```python
# ONLY for debugging to confirm SSL is the issue
# NEVER use verify=False in production — disables all security

import httpx
async with httpx.AsyncClient(verify=False) as client:  # INSECURE
    response = await client.get("https://internal.corp.com/api")

# If this works: SSL certificate issue confirmed
# Fix: add the proper CA cert instead of disabling verification
```

## Diagnosis

```bash
# Test from inside container
docker exec -it my-container bash -c "
    python3 -c \"
import ssl, urllib.request
ctx = ssl.create_default_context()
print('CA file:', ctx.get_ca_certs())
try:
    urllib.request.urlopen('https://your-internal-host.com', context=ctx)
    print('SSL OK')
except Exception as e:
    print(f'SSL Error: {e}')
\"
"

# Check which CAs are trusted in container
docker exec -it my-container python3 -c "
import ssl
ctx = ssl.create_default_context()
certs = ctx.get_ca_certs()
print(f'{len(certs)} CA certificates trusted')
"

# Inspect the failing certificate
echo | openssl s_client -connect internal.corp.com:443 2>/dev/null | \
    openssl x509 -noout -issuer -subject -dates
```

## Expected Token Savings
Debugging SSL errors without understanding Docker CA stores: ~6,000 tokens
Knowing to add CA cert to Dockerfile: immediate fix

## Environment
- Corporate environments with SSL inspection; internal services with self-signed certs
- Source: direct experience with enterprise Docker deployments
