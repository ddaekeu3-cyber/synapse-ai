---
title: "Agent Doesn't Implement Workload Identity for Cloud API Authentication"
description: "AI agents that use static long-lived API keys or IAM access key pairs to authenticate with cloud services create a wide blast radius when credentials are leaked and require manual rotation. Workload Identity (IRSA on AWS, Workload Identity Federation on GCP, Pod Identity on Azure) binds a Kubernetes service account or compute identity to a cloud IAM role, issuing short-lived tokens automatically without any static secrets."
date: 2025-02-20
difficulty: advanced
category: security
slug: agent-doesnt-implement-workload-identity-for-cloud-api-authentication
tags:
  - workload-identity
  - irsa
  - iam
  - cloud-security
  - zero-secrets
  - kubernetes
  - service-account
symptoms:
  - "AWS access keys checked into environment variables with 10-year expiry"
  - "Rotating a leaked API key requires redeploying all agent pods"
  - "Static GCP service account JSON key file mounted as a Kubernetes secret"
  - "Agent cannot call AWS S3 without AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in env"
  - "Security audit flags long-lived credentials in CI/CD pipeline environment variables"
---

## Problem

Static long-lived credentials (AWS access keys, GCP service account JSON, Azure client secrets) are the most common source of cloud security incidents for agent deployments. They accumulate in environment variables, CI/CD secrets, and container images; they have no automatic expiry; and when leaked, revocation requires immediate manual intervention across every system that holds a copy. Workload Identity solves all three: the cloud provider's identity plane issues short-lived OIDC tokens tied to the pod's service account, automatically refreshed, with no static secrets to leak. AWS calls this IRSA (IAM Roles for Service Accounts), GCP calls it Workload Identity Federation, and Azure calls it Pod Identity / Workload Identity.

---

## Solution 1: IRSACredentialProvider — AWS IRSA Token Exchange

```python
import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class IRSACredentialProvider:
    """
    Exchanges the projected Kubernetes service account token (mounted at
    AWS_WEB_IDENTITY_TOKEN_FILE) for temporary AWS credentials via
    STS AssumeRoleWithWebIdentity. The AWS SDK does this automatically
    when AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE are set, but this
    class makes the exchange explicit and auditable.

    Kubernetes setup required:
        - ServiceAccount annotated with eks.amazonaws.com/role-arn
        - Pod spec: serviceAccountName set, automountServiceAccountToken: true
        - IAM trust policy: allows sts:AssumeRoleWithWebIdentity from OIDC issuer

    Usage:
        provider = IRSACredentialProvider()
        session = provider.get_boto3_session()
        s3 = session.client("s3")
    """

    CREDENTIAL_EXPIRY_BUFFER = 300  # refresh 5 min before expiry

    def __init__(
        self,
        role_arn: Optional[str] = None,
        token_file: Optional[str] = None,
        session_name: str = "agent-irsa-session",
    ):
        self._role_arn = role_arn or os.environ.get("AWS_ROLE_ARN")
        self._token_file = token_file or os.environ.get(
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "/var/run/secrets/eks.amazonaws.com/serviceaccount/token",
        )
        self._session_name = session_name
        self._cached_creds: Optional[Dict[str, Any]] = None
        self._expiry: float = 0.0

    def _read_token(self) -> str:
        with open(self._token_file) as f:
            return f.read().strip()

    def _credentials_expired(self) -> bool:
        return time.time() >= self._expiry - self.CREDENTIAL_EXPIRY_BUFFER

    def _assume_role(self) -> Dict[str, Any]:
        import boto3
        token = self._read_token()
        sts = boto3.client("sts", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        resp = sts.assume_role_with_web_identity(
            RoleArn=self._role_arn,
            RoleSessionName=self._session_name,
            WebIdentityToken=token,
            DurationSeconds=3600,
        )
        creds = resp["Credentials"]
        logger.info(
            "irsa_credentials_refreshed expiry=%s role=%s",
            creds["Expiration"].isoformat(), self._role_arn,
        )
        return creds

    def get_credentials(self) -> Dict[str, str]:
        if self._cached_creds is None or self._credentials_expired():
            creds = self._assume_role()
            self._cached_creds = creds
            self._expiry = creds["Expiration"].timestamp()
        return {
            "aws_access_key_id": self._cached_creds["AccessKeyId"],
            "aws_secret_access_key": self._cached_creds["SecretAccessKey"],
            "aws_session_token": self._cached_creds["SessionToken"],
        }

    def get_boto3_session(self):
        import boto3
        creds = self.get_credentials()
        return boto3.Session(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            aws_session_token=creds["aws_session_token"],
        )
```

---

## Solution 2: GCPWorkloadIdentityProvider — GKE Workload Identity Token Exchange

```python
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class GCPWorkloadIdentityProvider:
    """
    Uses GKE Workload Identity to obtain a Google OAuth2 access token
    from the GKE metadata server without any service account JSON key file.
    The pod's Kubernetes service account is bound to a GCP service account
    via IAM annotations; the metadata server issues tokens automatically.

    Kubernetes setup required:
        - ServiceAccount annotated: iam.gke.io/gcp-service-account=<gsa>@<project>.iam.gserviceaccount.com
        - GCP IAM binding: roles/iam.workloadIdentityUser on the GSA

    Usage:
        provider = GCPWorkloadIdentityProvider(project="my-project")
        token = provider.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
    """

    METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    METADATA_HEADERS = {"Metadata-Flavor": "Google"}
    REFRESH_BUFFER = 120  # seconds before expiry to refresh

    def __init__(self, scopes: Optional[str] = None):
        self._scopes = scopes  # if set, request specific OAuth2 scopes
        self._token: Optional[str] = None
        self._expiry: float = 0.0

    def _fetch_token(self) -> dict:
        import urllib.request
        url = self.METADATA_URL
        if self._scopes:
            url += f"?scopes={self._scopes}"
        req = urllib.request.Request(url, headers=self.METADATA_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json
            return json.loads(resp.read())

    def get_access_token(self) -> str:
        if self._token is None or time.time() >= self._expiry - self.REFRESH_BUFFER:
            data = self._fetch_token()
            self._token = data["access_token"]
            self._expiry = time.time() + data.get("expires_in", 3600)
            logger.info("gcp_workload_identity_token_refreshed expires_in=%d", data.get("expires_in"))
        return self._token

    def get_credentials(self):
        """Return a google.oauth2.credentials.Credentials object."""
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            raise ImportError("pip install google-auth")
        token = self.get_access_token()
        return Credentials(token=token)

    async def get_access_token_async(self) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(None, self.get_access_token)
```

---

## Solution 3: AzureWorkloadIdentityProvider — Azure Pod Identity / Federated Credentials

```python
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class AzureWorkloadIdentityProvider:
    """
    Uses Azure Workload Identity (federated credential) to obtain an
    Azure AD access token. The pod's projected service account token is
    exchanged for an Azure AD token via the AZURE_FEDERATED_TOKEN_FILE
    environment variable set by the Azure Workload Identity mutating webhook.

    Kubernetes setup required:
        - Azure AD app registration with federated credential for OIDC issuer
        - Pod annotation: azure.workload.identity/use: "true"
        - AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE injected by webhook

    Usage:
        provider = AzureWorkloadIdentityProvider()
        token = provider.get_access_token("https://storage.azure.com/")
    """

    REFRESH_BUFFER = 300

    def __init__(
        self,
        client_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        token_file: Optional[str] = None,
    ):
        self._client_id = client_id or os.environ["AZURE_CLIENT_ID"]
        self._tenant_id = tenant_id or os.environ["AZURE_TENANT_ID"]
        self._token_file = token_file or os.environ.get(
            "AZURE_FEDERATED_TOKEN_FILE",
            "/var/run/secrets/azure/tokens/azure-identity-token",
        )
        self._cache: dict = {}  # scope -> (token, expiry)

    def _read_federated_token(self) -> str:
        with open(self._token_file) as f:
            return f.read().strip()

    def get_access_token(self, scope: str = "https://management.azure.com/.default") -> str:
        cached = self._cache.get(scope)
        if cached and time.time() < cached[1] - self.REFRESH_BUFFER:
            return cached[0]

        try:
            from azure.identity import ClientAssertionCredential
        except ImportError:
            raise ImportError("pip install azure-identity")

        def get_assertion():
            return self._read_federated_token()

        cred = ClientAssertionCredential(
            tenant_id=self._tenant_id,
            client_id=self._client_id,
            func=get_assertion,
        )
        token = cred.get_token(scope)
        self._cache[scope] = (token.token, token.expires_on)
        logger.info("azure_workload_identity_token_refreshed scope=%s", scope)
        return token.token
```

---

## Solution 4: WorkloadIdentityAuditLogger — Log All Token Exchange Events

```python
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class WorkloadIdentityAuditLogger:
    """
    Wraps any workload identity token provider and logs every token exchange
    as a structured audit event. Useful for SIEM ingestion: confirms that
    agents are using workload identity (not static credentials), tracks
    token refresh frequency, and alerts on unexpected role assumptions.

    Usage:
        provider = IRSACredentialProvider()
        audited = WorkloadIdentityAuditLogger(
            provider=provider,
            provider_name="irsa",
            agent_id="agent-rag-01",
        )
        session = audited.get_boto3_session()
    """

    def __init__(
        self,
        provider: Any,
        provider_name: str = "workload-identity",
        agent_id: str = "",
    ):
        self._provider = provider
        self._name = provider_name
        self._agent_id = agent_id
        self._exchange_count = 0
        self._last_exchange: Optional[float] = None

    def _audit(self, event: str, **fields):
        record = {
            "event": f"workload_identity_{event}",
            "provider": self._name,
            "agent_id": self._agent_id,
            "exchange_count": self._exchange_count,
            "ts": time.time(),
            **fields,
        }
        logger.info(json.dumps(record))

    def get_credentials(self) -> Dict[str, str]:
        now = time.time()
        creds = self._provider.get_credentials()
        self._exchange_count += 1
        self._audit(
            "token_exchanged",
            seconds_since_last=round(now - self._last_exchange, 1) if self._last_exchange else None,
        )
        self._last_exchange = now
        return creds

    def get_boto3_session(self):
        self.get_credentials()
        return self._provider.get_boto3_session()

    def get_access_token(self, scope: str = "") -> str:
        now = time.time()
        token = self._provider.get_access_token(scope) if scope else self._provider.get_access_token()
        self._exchange_count += 1
        self._audit("token_exchanged", scope=scope)
        self._last_exchange = now
        return token

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "provider": self._name,
            "exchange_count": self._exchange_count,
            "last_exchange": self._last_exchange,
        }
```

---

## Solution 5: WorkloadIdentityValidator — Startup Check for Correct Identity Configuration

```python
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class WorkloadIdentityValidator:
    """
    Validates that required workload identity environment variables and
    token files are present at startup. Fails fast with a clear error
    message instead of a cryptic 401 on the first API call.
    Supports AWS IRSA, GCP Workload Identity, and Azure Workload Identity.

    Usage:
        validator = WorkloadIdentityValidator.for_aws()
        issues = validator.validate()
        if issues:
            raise RuntimeError(f"Workload identity misconfigured: {issues}")
    """

    @dataclass_like_init = False  # pure dict-based for simplicity

    def __init__(self, required_env: List[str], required_files: List[str],
                  forbidden_env: Optional[List[str]] = None, provider: str = ""):
        self._required_env = required_env
        self._required_files = required_files
        self._forbidden_env = forbidden_env or []
        self._provider = provider

    @classmethod
    def for_aws(cls) -> "WorkloadIdentityValidator":
        return cls(
            required_env=["AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"],
            required_files=[os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE",
                                            "/var/run/secrets/eks.amazonaws.com/serviceaccount/token")],
            forbidden_env=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
            provider="AWS IRSA",
        )

    @classmethod
    def for_gcp(cls) -> "WorkloadIdentityValidator":
        return cls(
            required_env=[],
            required_files=[],
            forbidden_env=["GOOGLE_APPLICATION_CREDENTIALS"],
            provider="GCP Workload Identity",
        )

    @classmethod
    def for_azure(cls) -> "WorkloadIdentityValidator":
        return cls(
            required_env=["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE"],
            required_files=[os.environ.get("AZURE_FEDERATED_TOKEN_FILE", "")],
            forbidden_env=["AZURE_CLIENT_SECRET"],
            provider="Azure Workload Identity",
        )

    def validate(self) -> List[str]:
        issues = []
        for var in self._required_env:
            if not os.environ.get(var):
                issues.append(f"Missing required env var: {var}")
        for path in self._required_files:
            if path and not os.path.exists(path):
                issues.append(f"Token file not found: {path}")
        for var in self._forbidden_env:
            if os.environ.get(var):
                issues.append(f"Static credential detected in env: {var} — use {self._provider} instead")
        if issues:
            logger.error("workload_identity_validation_failed provider=%s issues=%s",
                          self._provider, issues)
        else:
            logger.info("workload_identity_validation_ok provider=%s", self._provider)
        return issues
```

---

## Solution 6: WorkloadIdentityAgentFactory — Unified Factory for Cloud-Agnostic Agent Setup

```python
import logging
import os
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CloudProvider(str, Enum):
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"
    AUTO = "auto"


class WorkloadIdentityAgentFactory:
    """
    Auto-detects the cloud environment and instantiates the correct
    workload identity provider. Returns a dict of authenticated clients
    (storage, secrets, etc.) ready for agent tools to use.
    No static secrets required in any supported environment.

    Usage:
        factory = WorkloadIdentityAgentFactory()
        clients = factory.build_clients()
        s3_client = clients.get("s3")
        secret = clients.get("secrets_manager").get_secret("my-secret")
    """

    def __init__(self, provider: CloudProvider = CloudProvider.AUTO):
        self._provider = provider if provider != CloudProvider.AUTO else self._detect()

    @staticmethod
    def _detect() -> CloudProvider:
        if os.environ.get("AWS_ROLE_ARN"):
            return CloudProvider.AWS
        if os.environ.get("AZURE_FEDERATED_TOKEN_FILE"):
            return CloudProvider.AZURE
        # GCP: try metadata server
        try:
            import urllib.request
            urllib.request.urlopen(
                "http://metadata.google.internal/computeMetadata/v1/",
                timeout=1,
            )
            return CloudProvider.GCP
        except Exception:
            pass
        raise RuntimeError(
            "Cannot detect cloud provider for workload identity. "
            "Set AWS_ROLE_ARN (AWS), AZURE_FEDERATED_TOKEN_FILE (Azure), "
            "or run on GKE with Workload Identity enabled."
        )

    def build_clients(self) -> Dict[str, Any]:
        if self._provider == CloudProvider.AWS:
            return self._build_aws_clients()
        if self._provider == CloudProvider.GCP:
            return self._build_gcp_clients()
        if self._provider == CloudProvider.AZURE:
            return self._build_azure_clients()
        raise ValueError(f"Unsupported provider: {self._provider}")

    def _build_aws_clients(self) -> Dict[str, Any]:
        provider = IRSACredentialProvider()
        session = provider.get_boto3_session()
        return {
            "s3": session.client("s3"),
            "secrets_manager": session.client("secretsmanager"),
            "ssm": session.client("ssm"),
            "provider": provider,
        }

    def _build_gcp_clients(self) -> Dict[str, Any]:
        provider = GCPWorkloadIdentityProvider()
        creds = provider.get_credentials()
        return {
            "credentials": creds,
            "token_fn": provider.get_access_token,
            "provider": provider,
        }

    def _build_azure_clients(self) -> Dict[str, Any]:
        provider = AzureWorkloadIdentityProvider()
        return {
            "token_fn": provider.get_access_token,
            "provider": provider,
        }
```

---

## Comparison

| Approach | Cloud | Static Secrets | Auto-Refresh | Token Lifetime | Audit Logging | Startup Validation |
|---|---|---|---|---|---|---|
| **IRSACredentialProvider** | AWS | None | Yes | 1 hour | No | No |
| **GCPWorkloadIdentityProvider** | GCP | None | Yes | 1 hour | No | No |
| **AzureWorkloadIdentityProvider** | Azure | None | Yes | Configurable | No | No |
| **WorkloadIdentityAuditLogger** | Any | N/A | Delegates | N/A | Yes | No |
| **WorkloadIdentityValidator** | Any | N/A | N/A | N/A | No | Yes |
| **WorkloadIdentityAgentFactory** | Auto-detect | None | Yes | Per provider | No | Implicit |

**Key insight**: the single most impactful change is annotating the Kubernetes ServiceAccount with the IAM role ARN (`eks.amazonaws.com/role-arn` on AWS) and removing `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from the pod spec. The AWS SDK automatically detects `AWS_ROLE_ARN` + `AWS_WEB_IDENTITY_TOKEN_FILE` and performs the STS exchange without any code change. Add `WorkloadIdentityValidator.for_aws().validate()` to the agent startup sequence to fail fast when misconfigured. In a post-incident review, `WorkloadIdentityAuditLogger` provides evidence that no static credential was used during the incident window—a compliance requirement in SOC 2 and ISO 27001 audits.
