---
layout: solution
title: "Service Account Gets 403 Forbidden — Insufficient Permissions for Agent Operations"
category: auth
description: "Agent uses a service account or API key that doesn't have permission for the operation it needs. Gets 403 Forbidden. Account exists and authenticates, but lacks the specific IAM role or scope."
tags: [403, permissions, service-account, iam, oauth-scope, authorization]
---

# Service Account Gets 403 Forbidden — Insufficient Permissions for Agent Operations

## Symptom
- HTTP 403 Forbidden on specific API calls
- Agent authenticates successfully (no 401) but gets 403 on certain operations
- Error: `403: Caller does not have permission` or `insufficient_scope`
- Works in development (using personal credentials) but fails in production (service account)
- Some operations succeed, others fail — permission is scope-dependent

## Root Cause
Authentication (who you are) succeeded but authorization (what you can do) failed. Service accounts are often created with minimal permissions and need explicit grants for each operation. Common causes: missing IAM role, OAuth scope not requested, resource-level permission not set, or organization policy blocking the action.

## Fix

### Option 1: Identify exactly which permission is missing

```bash
# Google Cloud — check what permission is required
gcloud projects get-iam-policy PROJECT_ID

# Check service account's current roles
gcloud iam service-accounts get-iam-policy SA_EMAIL

# Use Policy Troubleshooter to find missing permission
gcloud policy-troubleshoot iam "//cloudresourcemanager.googleapis.com/projects/PROJECT_ID" \
    --principal "serviceAccount:SA_EMAIL" \
    --permission "storage.objects.create"

# AWS — simulate policy to find missing permission
aws iam simulate-principal-policy \
    --policy-source-arn arn:aws:iam::ACCOUNT:role/ROLE_NAME \
    --action-names s3:PutObject \
    --resource-arns arn:aws:s3:::bucket-name/*
```

### Option 2: Principle of least privilege — grant minimum required

```python
# Document required permissions at the top of your agent code
REQUIRED_PERMISSIONS = {
    "google_cloud": [
        "storage.objects.create",    # Write files to GCS
        "storage.objects.get",       # Read files from GCS
        "bigquery.tables.getData",   # Read BigQuery
        "pubsub.topics.publish",     # Publish messages
    ],
    "aws": [
        "s3:GetObject",
        "s3:PutObject",
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
    ]
}

def check_permissions(service: str) -> list[str]:
    """Test that all required permissions are available at startup"""
    import boto3
    iam = boto3.client("iam")
    missing = []

    for perm in REQUIRED_PERMISSIONS.get(service, []):
        response = iam.simulate_principal_policy(
            PolicySourceArn=get_current_role_arn(),
            ActionNames=[perm],
            ResourceArns=["*"]
        )
        if response["EvaluationResults"][0]["EvalDecision"] != "allowed":
            missing.append(perm)

    return missing
```

### Option 3: Handle 403 with helpful error message

```python
import httpx

async def api_call_with_permission_help(url: str, method: str = "GET", **kwargs) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.request(method, url, **kwargs)

    if response.status_code == 403:
        body = response.text[:500]
        # Parse common 403 error patterns
        if "insufficient_scope" in body:
            required_scope = _extract_required_scope(body)
            raise PermissionError(
                f"403 Forbidden: OAuth token missing required scope.\n"
                f"Required scope: {required_scope}\n"
                f"Add this scope when requesting the access token."
            )
        elif "does not have permission" in body:
            raise PermissionError(
                f"403 Forbidden: Service account lacks required IAM permission.\n"
                f"Error: {body}\n"
                f"Check IAM roles for your service account in the cloud console."
            )
        else:
            raise PermissionError(
                f"403 Forbidden accessing {url}.\n"
                f"Response: {body}\n"
                f"Check: IAM roles, OAuth scopes, resource-level permissions."
            )

    response.raise_for_status()
    return response.json()
```

### Option 4: OAuth — request all needed scopes upfront

```python
# WRONG — minimal scope, will get 403 on some operations
SCOPES = ["https://www.googleapis.com/auth/cloud-platform.read-only"]

# RIGHT — request all scopes your agent will need
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",         # GCP access
    "https://www.googleapis.com/auth/bigquery",               # BigQuery
    "https://www.googleapis.com/auth/devstorage.read_write", # GCS read/write
    "https://www.googleapis.com/auth/gmail.readonly",         # Gmail read
]

from google.oauth2 import service_account
import google.auth.transport.requests

def get_credentials(service_account_file: str) -> service_account.Credentials:
    creds = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=SCOPES  # All required scopes declared here
    )
    # Refresh to verify scopes were granted
    creds.refresh(google.auth.transport.requests.Request())
    return creds
```

### Option 5: IAM policy templates for common agent operations

```json
// AWS IAM policy for typical agent operations
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AgentS3Access",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::agent-data-bucket",
        "arn:aws:s3:::agent-data-bucket/*"
      ]
    },
    {
      "Sid": "AgentSQSAccess",
      "Effect": "Allow",
      "Action": [
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:us-east-1:*:agent-task-queue"
    },
    {
      "Sid": "AgentSecretsAccess",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:us-east-1:*:secret:agent/*"
    }
  ]
}
```

### Option 6: Test permissions in staging before production

```python
async def run_permission_preflight(operations: list[dict]) -> list[str]:
    """Test each required operation before starting the agent"""
    failures = []

    for op in operations:
        try:
            await op["test_fn"]()
            print(f"✓ {op['name']}: permission OK")
        except PermissionError as e:
            failures.append(f"✗ {op['name']}: PERMISSION DENIED — {e}")
        except Exception as e:
            failures.append(f"✗ {op['name']}: {type(e).__name__}: {e}")

    if failures:
        print("\nPermission failures:")
        for f in failures:
            print(f"  {f}")
        raise RuntimeError("Agent cannot start — insufficient permissions")

# Run before starting agent
await run_permission_preflight([
    {"name": "Read from S3", "test_fn": lambda: s3.get_object(Bucket="bucket", Key="test")},
    {"name": "Write to S3", "test_fn": lambda: s3.put_object(Bucket="bucket", Key="test", Body=b"")},
    {"name": "Send SQS message", "test_fn": lambda: sqs.send_message(QueueUrl=QUEUE_URL, MessageBody="test")},
])
```

## 403 vs 401 Diagnosis

| Status | Meaning | Fix |
|---|---|---|
| 401 Unauthorized | Not authenticated | Check API key / token |
| 403 Forbidden | Authenticated but not authorized | Add IAM role / OAuth scope |
| 403 + "insufficient_scope" | OAuth scope missing | Add scope to token request |
| 403 + "does not have permission" | Missing IAM role | Grant specific IAM permission |
| 403 + organization policy | Org policy blocking | Contact GCP/AWS admin |

## Expected Token Savings
Debugging mystery 403 errors without understanding IAM: ~8,000 tokens
Preflight permission check catches it before any work starts: 0 wasted

## Environment
- Any agent using cloud service accounts, especially Google Cloud and AWS
- Source: direct experience with production service account permission issues
