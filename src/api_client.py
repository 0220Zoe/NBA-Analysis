"""Small standard-library HTTP client for the vendor API."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    """HTTP, JSON, or application-level API failure."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ApiResponse:
    endpoint: str
    status: int
    payload: Any


class NBAApiClient:
    """Authenticated JSON client with bounded retries for transient failures."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 20.0,
        retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._authorization = f"Basic {token}"

    def get(self, path: str, params: Optional[Dict[str, str]] = None) -> ApiResponse:
        endpoint = path
        if params:
            endpoint = f"{path}?{urlencode(params)}"
        url = f"{self.base_url}{endpoint}"

        last_error: Optional[ApiError] = None
        for attempt in range(self.retries + 1):
            request = Request(
                url,
                headers={
                    "Authorization": self._authorization,
                    "Accept": "application/json",
                    "User-Agent": "nba-coaching-pipeline/1.0",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    body = response.read().decode("utf-8")
                payload = json.loads(body)
                # The OpenAPI document allows ErrorResponse with HTTP 200.
                if isinstance(payload, dict) and ("error" in payload or "detail" in payload):
                    raise ApiError(
                        f"application error from {endpoint}: "
                        f"{payload.get('error') or payload.get('detail')}",
                        status=status,
                    )
                return ApiResponse(endpoint, status, payload)
            except HTTPError as exc:
                last_error = ApiError(
                    f"HTTP {exc.code} from {endpoint}: {exc.reason}", status=exc.code
                )
                retryable = exc.code == 429 or 500 <= exc.code < 600
            except (URLError, TimeoutError) as exc:
                last_error = ApiError(f"network error from {endpoint}: {exc}")
                retryable = True
            except json.JSONDecodeError as exc:
                raise ApiError(f"invalid JSON from {endpoint}: {exc}") from exc
            except ApiError as exc:
                last_error = exc
                retryable = exc.status is None or exc.status == 429 or exc.status >= 500

            if not retryable or attempt == self.retries:
                assert last_error is not None
                raise last_error
            time.sleep(0.5 * (2**attempt))

        raise AssertionError("retry loop exited unexpectedly")
