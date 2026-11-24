from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from sncf_mcp.gtfsrt import PARIS

FMT = "%Y-%m-%d %H:%M"
DEFAULT_URL = "https://api.sncf.com/v1/coverage/sncf"


class SncfApiError(ToolError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SncfApi:
    def __init__(self, client: httpx.AsyncClient | None) -> None:
        self.client = client

    @classmethod
    def create(cls, url: str, token: str | None) -> Self:
        if not token:
            return cls(None)
        return cls(httpx.AsyncClient(base_url=url, auth=(token, ""), timeout=httpx.Timeout(10.0)))

    async def aclose(self) -> None:
        if self.client:
            await self.client.aclose()

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            raise SncfApiError("SNCF_API_TOKEN is not set on the server")
        try:
            resp = await self.client.get(path, params=params)
        except httpx.TimeoutException:
            raise SncfApiError("The SNCF API timed out, retry shortly") from None
        except httpx.HTTPError:
            raise SncfApiError("The SNCF API is unreachable") from None
        if resp.status_code in (401, 403):
            raise SncfApiError("The SNCF API rejected the token")
        if resp.status_code == 404:
            raise SncfApiError(_message(resp) or "Nothing found for this request", 404)
        if resp.status_code == 429:
            raise SncfApiError("SNCF API quota exceeded, retry later")
        if resp.status_code >= 400:
            raise SncfApiError(_message(resp) or f"SNCF API error {resp.status_code}")
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            raise SncfApiError("The SNCF API returned an invalid response") from None
        return data

    async def get_or_none(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return await self.get(path, params)
        except SncfApiError as e:
            if e.status == 404:
                return None
            raise


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        error = body.get("error")
        nested = error.get("message") if isinstance(error, dict) else None
        return str(nested or body.get("message") or "")
    except (ValueError, AttributeError):
        return ""


def to_api(value: str) -> str:
    dt = datetime.fromisoformat(value)
    dt = dt.replace(tzinfo=PARIS) if dt.tzinfo is None else dt.astimezone(PARIS)
    return dt.strftime("%Y%m%dT%H%M%S")


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def shift(value: str | None, minutes: int) -> str | None:
    dt = parse(value)
    return (dt + timedelta(minutes=minutes)).strftime(FMT) if dt else None


def local(value: str | None) -> str | None:
    return shift(value, 0)


def utc_to_paris(value: str | None) -> str | None:
    dt = parse(value)
    return dt.replace(tzinfo=UTC).astimezone(PARIS).strftime(FMT) if dt else None


def minutes(seconds: float) -> int:
    return int(seconds // 60)


def delay_min(base: str | None, actual: str | None) -> int:
    start, end = parse(base), parse(actual)
    return minutes((end - start).total_seconds()) if start and end else 0
