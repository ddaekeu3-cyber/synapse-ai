---
layout: solution
title: "API Key Accidentally Committed to Git — Secret Leaked in Repository"
category: auth
description: "Agent code with hardcoded API key got committed to git. Key appears in git history even after deleting the file. Must rotate key and clean git history to prevent abuse."
tags: [api-key, secrets, git, security, leaked-credentials, rotation]
---

# API Key Accidentally Committed to Git — Secret Leaked in Repository

## Symptom
- Hardcoded `ANTHROPIC_API_KEY = "sk-ant-..."` appears in committed file
- GitHub sent a "secret scanning" alert email
- Someone else is using your API quota unexpectedly
- Key still visible in `git log` even after you deleted it in a new commit
- CI/CD exposed secret via `echo $API_KEY` in logs

## Root Cause
Git stores the full history. Deleting a file in a new commit doesn't remove it from history — the key is still in every previous commit. If the repo is public (or was ever public for a moment), the key should be considered compromised regardless of whether you see evidence of misuse.

## Fix

### Step 1: Rotate the key immediately (do this first)

```bash
# 1. Go to https://console.anthropic.com/settings/keys
# 2. Revoke the leaked key
# 3. Create a new key
# 4. Update your secrets manager / environment variables

# Never reuse the rotated key in the same place it was leaked
```

Rotating before cleaning history ensures any attacker using the leaked key is cut off immediately.

### Step 2: Move the key to environment variables

```python
# WRONG — never hardcode secrets
ANTHROPIC_API_KEY = "sk-ant-api03-..."

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# RIGHT — read from environment
import os
import anthropic

client = anthropic.Anthropic()  # Reads ANTHROPIC_API_KEY env var automatically
# Or explicitly:
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
```

```bash
# Local development: .env file (never committed)
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
echo ".env" >> .gitignore
```

### Step 3: Clean git history

```bash
# Option A: git filter-repo (recommended, faster than filter-branch)
pip install git-filter-repo

git filter-repo --path config.py --invert-paths  # Remove file entirely
# OR replace the secret string:
git filter-repo --replace-text <(echo 'sk-ant-api03-XXXX==>REDACTED')

# Option B: BFG Repo Cleaner (Java, simpler for secrets)
# Download: https://rtyley.github.io/bfg-repo-cleaner/
bfg --replace-text secrets.txt  # secrets.txt: "sk-ant-api03-XXXX"
git reflog expire --expire=now --all
git gc --prune=now --aggressive
git push --force-with-lease
```

### Step 4: Add pre-commit hooks to prevent future leaks

```bash
# Install pre-commit
pip install pre-commit

# .pre-commit-config.yaml
cat > .pre-commit-config.yaml << 'EOF'
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks
EOF

pre-commit install
```

```bash
# Quick gitleaks scan on current repo
docker run -v $(pwd):/path zricethezav/gitleaks:latest detect --source /path -v
```

### Step 5: Verify the secret is gone from history

```bash
# Search git history for the leaked string
git log --all -p | grep "sk-ant"
# Should return nothing after cleaning

# Search all objects
git grep "sk-ant" $(git rev-list --all)
# Should return nothing
```

### Option 6: Use a secrets manager (production)

```python
# AWS Secrets Manager
import boto3, json

def get_secret(secret_name: str) -> str:
    client = boto3.client('secretsmanager', region_name='us-east-1')
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response['SecretString'])['ANTHROPIC_API_KEY']

api_key = get_secret("prod/agent/anthropic")
client = anthropic.Anthropic(api_key=api_key)
```

```python
# HashiCorp Vault
import hvac

vault = hvac.Client(url='https://vault.internal', token=os.environ['VAULT_TOKEN'])
secret = vault.secrets.kv.v2.read_secret_version(path='agent/anthropic')
api_key = secret['data']['data']['api_key']
```

## Checklist After a Secret Leak

| Step | Action | Done? |
|------|--------|-------|
| 1 | Revoke leaked key in Anthropic console | ☐ |
| 2 | Issue new key | ☐ |
| 3 | Update all services using old key | ☐ |
| 4 | Clean git history with filter-repo/BFG | ☐ |
| 5 | Force-push cleaned history | ☐ |
| 6 | Verify secret gone from all history | ☐ |
| 7 | Add pre-commit secret scanning hook | ☐ |
| 8 | Check API usage logs for abuse during exposure | ☐ |

## Expected Token Savings
This isn't about token savings — it's about preventing unauthorized API usage that burns your quota and incurs billing.

## Environment
- Any git repository, especially public GitHub repos
- Source: GitHub secret scanning alerts, direct experience with leaked API keys
