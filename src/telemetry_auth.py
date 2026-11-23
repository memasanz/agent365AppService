"""Managed identity -> blueprint FMI assertion -> child app-only Observability token."""

import base64
import json
import logging
import math
import threading
import time
from uuid import UUID

import requests
from azure.identity import ManagedIdentityCredential

FMI_SCOPE = "api://AzureADTokenExchange/.default"
OBSERVABILITY_RESOURCE = "9b975845-388f-4429-889e-eab1ef63949c"
OBSERVABILITY_SCOPE = f"api://{OBSERVABILITY_RESOURCE}/.default"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
logger = logging.getLogger(__name__)


def guid(value, setting):
    try:
        result = UUID(value)
        if not result.int:
            raise ValueError
        return str(result)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{setting} must be a nonzero GUID.") from None


class TelemetryTokenError(RuntimeError):
    """Sanitized acquisition failure; no token or response body is included."""


class ObservabilityTokenResolver:
    """Synchronous exporter callback, serialized cache for one tenant/child pair."""

    def __init__(
        self,
        tenant_id,
        child_id,
        blueprint_client_id,
        managed_identity_client_id,
        *,
        credential=None,
        session=None,
        clock=time.time,
    ):
        self.tenant_id = guid(tenant_id, "ENTRA_TENANT_ID")
        self.child_id = guid(child_id, "ENTRA_AGENT_IDENTITY_OBJECT_ID")
        self.blueprint_id = guid(blueprint_client_id, "ENTRA_BLUEPRINT_CLIENT_ID")
        self.managed_identity_id = guid(
            managed_identity_client_id,
            "AZURE_MANAGED_IDENTITY_CLIENT_ID",
        )
        if self.child_id == self.blueprint_id:
            raise ValueError(
                "Use the child identity, not the blueprint, for telemetry attribution."
            )
        self.credential = credential or ManagedIdentityCredential(
            client_id=self.managed_identity_id
        )
        self.session = session or requests.Session()
        self.clock = clock
        self.lock = threading.Lock()
        self.token = None
        self.expires_at = 0
        self.retry_after = 0

    def _exchange(self, fields, stage):
        try:
            response = self.session.post(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", **fields},
                timeout=(5, 25),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise TelemetryTokenError(
                    f"Observability {stage} exchange failed (HTTP {response.status_code}). "
                    "Check FIC, child registration and tenant policy; no consent fallback is used."
                )
            result = response.json()
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("access_token"), str)
                or not result["access_token"]
                or str(result.get("token_type", "")).lower() != "bearer"
            ):
                raise ValueError
            return result
        except TelemetryTokenError:
            raise
        except Exception:
            raise TelemetryTokenError(
                f"Observability {stage} exchange failed; check connectivity and federation."
            ) from None

    def _validate(self, result, requested_at):
        try:
            encoded = result["access_token"].split(".")
            if len(encoded) != 3 or not all(encoded):
                raise ValueError
            claims = json.loads(base64.urlsafe_b64decode(encoded[1] + "=" * (-len(encoded[1]) % 4)))
            roles = claims.get("roles", [])
            app_only = claims.get("idtyp") == "app" or (
                "idtyp" not in claims
                and (
                    bool(roles)
                    or bool(claims.get("oid"))
                    and claims.get("oid") == claims.get("sub")
                )
            )
            clients = [claims[k] for k in ("azp", "appid") if k in claims]
            if (
                not app_only
                or "scp" in claims
                or not isinstance(roles, list)
                or any(not isinstance(r, str) or not r.strip() for r in roles)
                or claims.get("aud")
                not in (
                    OBSERVABILITY_RESOURCE,
                    f"api://{OBSERVABILITY_RESOURCE}",
                )
                or guid(claims.get("tid"), "token tenant") != self.tenant_id
                or not clients
                or any(guid(c, "token client") != self.child_id for c in clients)
            ):
                raise ValueError
            expiries = []
            for value, offset in (
                (claims["exp"], 0),
                (result["expires_in"], requested_at),
            ):
                if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                    raise ValueError
                expiries.append(float(value) + offset)
            expiry = min(expiries)
            if expiry <= self.clock() + 60:
                raise ValueError
            return result["access_token"], expiry
        except Exception:
            raise TelemetryTokenError(
                "Observability token is not a usable app-only token for the configured "
                "tenant, child and audience. No token was cached."
            ) from None

    def __call__(self, agent_id, tenant_id):
        if (
            guid(agent_id, "export child") != self.child_id
            or guid(tenant_id, "export tenant") != self.tenant_id
        ):
            raise TelemetryTokenError("Cross-identity telemetry export refused.")
        with self.lock:
            if self.token and self.clock() < self.expires_at - 60:
                return self.token
            if self.clock() < self.retry_after:
                raise TelemetryTokenError("Observability token refresh temporarily backed off.")
            self.token, self.expires_at = None, 0
            try:
                assertion = self.credential.get_token(FMI_SCOPE).token
                parent = self._exchange(
                    {
                        "client_id": self.blueprint_id,
                        "client_assertion_type": ASSERTION_TYPE,
                        "client_assertion": assertion,
                        "scope": FMI_SCOPE,
                        "fmi_path": self.child_id,
                    },
                    "blueprint FMI",
                )
                requested_at = self.clock()
                child = self._exchange(
                    {
                        "client_id": self.child_id,
                        "client_assertion_type": ASSERTION_TYPE,
                        "client_assertion": parent["access_token"],
                        "scope": OBSERVABILITY_SCOPE,
                    },
                    "child",
                )
                # Routing/type guards only; Entra TLS supplies the JWT and the service verifies it.
                self.token, self.expires_at = self._validate(child, requested_at)
                self.retry_after = 0
                logger.info("Observability app-only token acquired; delivery not yet verified.")
                return self.token
            except Exception:
                self.retry_after = self.clock() + 5
                logger.warning(
                    "Observability token acquisition failed. Check managed identity, exact FIC, "
                    "child registration and service policy. Model requests remain available."
                )
                raise TelemetryTokenError("Observability token acquisition failed.") from None

    def close(self):
        with self.lock:
            self.token = None
            self.session.close()
            self.credential.close()
