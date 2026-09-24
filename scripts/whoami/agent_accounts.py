"""Claude and Codex account identity and weekly quota, shared by whoami and agent_notes.

Two kinds of source, and callers pick by cost:

- live: `claude auth status` plus the OAuth usage endpoint, and `codex app-server`
  over stdio. Authoritative, but each call spawns a CLI or hits the network.
- on disk: what the CLIs already wrote for the account logged in right now. Claude
  Code keeps `oauthAccount` and a `cachedUsageUtilization` snapshot in its
  `.claude.json`; Codex keeps an OIDC id_token in `auth.json` whose payload carries
  the email. Free to read, so safe on a two-minute timer.

These default credential stores describe the current login. Historical usage
is not a live reading of an inactive account; agent_notes preserves observations
made while an account was active without retaining its credentials.

Nothing here returns or prints a credential: tokens are read only to be sent to
the endpoint that issued them, or decoded for the email claim.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import http.client
import json
import os
import math
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast
from zoneinfo import ZoneInfo

WEEK_MINUTES = 7 * 24 * 60
EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Quota:
    label: str
    used_percent: float | None
    resets_at: datetime | None

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None or not math.isfinite(self.used_percent):
            return None
        return max(0, min(100, 100 - self.used_percent))


@dataclass
class Report:
    """One tool's account. `problem` replaces the account lines; `quota_problem` follows them."""

    tool: str
    email: str | None = None
    plan: str | None = None
    quotas: list[Quota] = field(default_factory=list)
    problem: str | None = None
    quota_problem: str | None = None
    limit_reset_count: int | None = None
    limit_reset_expirations: list[datetime] = field(default_factory=list)

    @property
    def weekly_reset(self) -> datetime | None:
        """The main weekly window's reset, not a per-model one."""
        for quota in self.quotas:
            if quota.label == "Weekly":
                return quota.resets_at
        return None

    @property
    def weekly_remaining(self) -> float | None:
        for quota in self.quotas:
            if quota.label == "Weekly":
                return quota.remaining_percent
        return None


# --- JSON records -------------------------------------------------------------


class ClaudeAuthStatus(TypedDict, total=False):
    loggedIn: bool
    email: str
    subscriptionType: str
    authMethod: str


class ClaudeOauth(TypedDict, total=False):
    accessToken: str


class ClaudeCredentials(TypedDict, total=False):
    claudeAiOauth: ClaudeOauth


class UsageWindow(TypedDict, total=False):
    utilization: float | None
    resets_at: str | None


class OauthAccount(TypedDict, total=False):
    accountUuid: str
    emailAddress: str


class CachedUsage(TypedDict, total=False):
    accountUuid: str
    fetchedAtMs: int
    utilization: dict[str, object]


class ClaudeConfig(TypedDict, total=False):
    oauthAccount: OauthAccount
    cachedUsageUtilization: CachedUsage


class CodexTokens(TypedDict, total=False):
    id_token: str


class CodexAuth(TypedDict, total=False):
    auth_mode: str
    tokens: CodexTokens


class IdTokenClaims(TypedDict, total=False):
    email: str


class CodexAccount(TypedDict, total=False):
    type: str
    email: str
    planType: str


class CodexAccountResult(TypedDict, total=False):
    account: CodexAccount | None


class CodexWindow(TypedDict, total=False):
    usedPercent: float
    windowDurationMins: int
    resetsAt: int | None


class CodexBucket(TypedDict, total=False):
    primary: CodexWindow | None
    secondary: CodexWindow | None


class CodexRateLimits(TypedDict, total=False):
    rateLimits: CodexBucket
    rateLimitsByLimitId: dict[str, CodexBucket] | None


class RpcError(TypedDict, total=False):
    message: str


class RpcResponse(TypedDict, total=False):
    id: int
    result: dict[str, object]
    error: RpcError


# --- shared helpers ----------------------------------------------------------


def parse_reset(value: object) -> datetime | None:
    """A reset as an aware datetime, from epoch seconds or an ISO string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def executable(name: str) -> str:
    """The snapshot timer has a smaller PATH than an interactive shell."""
    found = shutil.which(name)
    if found:
        return found
    for directory in (Path.home() / ".local/bin", Path.home() / ".cargo/bin"):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return name


def command_json(*args: str) -> object:
    result = subprocess.run((executable(args[0]), *args[1:]), capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("account command failed")
    return cast(object, json.loads(result.stdout))


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))


def claude_config_file() -> Path:
    """Claude Code's `.claude.json`: inside CLAUDE_CONFIG_DIR when set, else in home."""
    if "CLAUDE_CONFIG_DIR" in os.environ:
        return claude_config_dir() / ".claude.json"
    return Path.home() / ".claude.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


# --- Claude ------------------------------------------------------------------


def claude_credentials() -> ClaudeCredentials:
    path = claude_config_dir() / ".credentials.json"
    if path.exists():
        return cast(ClaudeCredentials, json.loads(path.read_text()))
    # Claude Code uses the macOS login keychain for its default configuration.
    if sys.platform == "darwin" and "CLAUDE_CONFIG_DIR" not in os.environ:
        return cast(ClaudeCredentials, command_json(
            "security", "find-generic-password", "-s", "Claude Code-credentials", "-w"))
    raise RuntimeError("usage credentials unavailable")


def claude_usage_quotas(usage: dict[str, object]) -> list[Quota]:
    """The weekly windows in a Claude usage payload, main window first."""
    quotas: list[Quota] = []
    weekly = usage.get("seven_day")
    if isinstance(weekly, dict):
        window = cast(UsageWindow, cast(object, weekly))
        quotas.append(Quota("Weekly", window.get("utilization"), parse_reset(window.get("resets_at"))))
    for name, value in usage.items():
        if name.startswith("seven_day_") and isinstance(value, dict) and "utilization" in value:
            window = cast(UsageWindow, cast(object, value))
            label = "Weekly " + name.removeprefix("seven_day_").replace("_", " ")
            quotas.append(Quota(label, window.get("utilization"), parse_reset(window.get("resets_at"))))
    return quotas


def claude_live() -> Report:
    report = Report("Claude")
    try:
        account = cast(ClaudeAuthStatus, command_json("claude", "auth", "status"))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        report.problem = "Account unavailable (claude auth status failed)"
        return report
    if not account.get("loggedIn"):
        report.problem = "Not logged in"
        return report
    report.email = account.get("email")
    report.plan = account.get("subscriptionType") or account.get("authMethod")
    if account.get("authMethod") != "claude.ai":
        report.quota_problem = "Weekly quota: unavailable for this authentication method"
        return report
    try:
        token = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                 or claude_credentials().get("claudeAiOauth", {}).get("accessToken"))
        if not token:
            raise RuntimeError("usage credentials unavailable")
        request = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        opened = cast(http.client.HTTPResponse, urllib.request.urlopen(request, timeout=15))
        with opened as response:
            usage = cast(dict[str, object], json.loads(response.read()))
        report.quotas = claude_usage_quotas(usage)
        if not any(quota.label == "Weekly" for quota in report.quotas):
            report.quota_problem = "Weekly quota: unavailable"
    except urllib.error.HTTPError as error:
        report.quota_problem = f"Weekly quota: unavailable (HTTP {error.code}; try Claude /usage)"
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        report.quota_problem = "Weekly quota: unavailable (could not read live usage; try Claude /usage)"
    return report


def claude_on_disk() -> Report:
    """The logged-in Claude account and the usage Claude Code last cached for it.

    The cache is trusted only when its accountUuid is the logged-in account's, so
    a snapshot left over from before an account switch is never attributed to the
    account that replaced it.
    """
    report = Report("Claude")
    try:
        config = cast(ClaudeConfig, json.loads(claude_config_file().read_text()))
    except (OSError, ValueError):
        report.problem = "Account unavailable (could not read Claude config)"
        return report
    account = config.get("oauthAccount")
    if not account or not account.get("emailAddress"):
        report.problem = "Not logged in"
        return report
    report.email = account.get("emailAddress")
    cached = config.get("cachedUsageUtilization")
    if cached and cached.get("accountUuid") == account.get("accountUuid"):
        report.quotas = claude_usage_quotas(cached.get("utilization", {}))
    else:
        report.quota_problem = "Weekly quota: no cached usage for this account"
    return report


# --- Codex -------------------------------------------------------------------


def codex_email_on_disk() -> str | None:
    """The email claim of the id_token in Codex's auth.json, if logged in with ChatGPT."""
    try:
        auth = cast(CodexAuth, json.loads((codex_home() / "auth.json").read_text()))
    except (OSError, ValueError):
        return None
    token = auth.get("tokens", {}).get("id_token", "")
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = cast(IdTokenClaims, json.loads(base64.urlsafe_b64decode(payload)))
    except (binascii.Error, ValueError):
        return None
    return claims.get("email") or None


class CodexServer:
    """A `codex app-server` over stdio, spoken to one JSON-RPC request at a time."""

    process: asyncio.subprocess.Process

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    async def send(self, message: dict[str, object]) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise RuntimeError("account API has no stdin")
        stdin.write((json.dumps(message) + "\n").encode())
        await stdin.drain()

    async def request(self, identifier: int, method: str, params: dict[str, object]) -> object:
        await self.send({"id": identifier, "method": method, "params": params})
        stdout = self.process.stdout
        if stdout is None:
            raise RuntimeError("account API has no stdout")

        async def receive() -> object:
            while raw := await stdout.readline():
                response = cast(RpcResponse, json.loads(raw))
                if response.get("id") == identifier:
                    if "error" in response:
                        raise RuntimeError("account API request failed")
                    return cast(object, response.get("result", {}))
            raise RuntimeError("account API closed")

        return await asyncio.wait_for(receive(), timeout=20)

    async def close(self) -> None:
        if self.process.returncode is not None:
            return
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        try:
            _ = await asyncio.wait_for(self.process.wait(), timeout=3)
        except asyncio.TimeoutError:
            self.process.kill()
            _ = await self.process.wait()


def codex_weekly_quotas(usage: CodexRateLimits) -> list[Quota]:
    buckets = usage.get("rateLimitsByLimitId") or {"codex": usage.get("rateLimits", {})}
    quotas: list[Quota] = []
    for name, bucket in buckets.items():
        for window in (bucket.get("primary"), bucket.get("secondary")):
            if window and window.get("windowDurationMins") == WEEK_MINUTES:
                label = "Weekly" if name == "codex" else f"Weekly ({name})"
                quotas.append(Quota(label, window.get("usedPercent"), parse_reset(window.get("resetsAt"))))
    return quotas


async def codex_live() -> Report:
    report = Report("Codex")
    server: CodexServer | None = None
    try:
        server = CodexServer(await asyncio.create_subprocess_exec(
            executable("codex"), "app-server", "--listen", "stdio://",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        ))
        _ = await server.request(1, "initialize", {"clientInfo": {"name": "whoami", "version": "1.0"}})
        await server.send({"method": "initialized"})
        result = cast(CodexAccountResult, await server.request(2, "account/read", {"refreshToken": False}))
        account = result.get("account")
        if not account:
            report.problem = "Not logged in"
            return report
        report.email = account.get("email") or account.get("type")
        report.plan = account.get("planType")
        if account.get("type") != "chatgpt":
            report.quota_problem = "Weekly quota: unavailable for this authentication method"
            return report
        try:
            usage = cast(CodexRateLimits, await server.request(3, "account/rateLimits/read", {}))
            report.quotas = codex_weekly_quotas(usage)
            reset_credits(report, cast(dict[str, object], usage).get("rateLimitResetCredits"))
            if not report.quotas:
                report.quota_problem = "Weekly quota: unavailable (no weekly window returned)"
        except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError):
            report.quota_problem = "Weekly quota: unavailable (Codex usage API failed)"
    except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError):
        if report.email is None:
            report.problem = "Account/weekly quota unavailable (Codex account API failed)"
        else:
            report.quota_problem = "Weekly quota: unavailable (Codex usage API failed)"
    finally:
        if server is not None:
            await server.close()
    return report


def reset_credits(report: Report, summary: object) -> None:
    """Count comes from the summary; detail rows can be absent or capped."""
    if not isinstance(summary, dict):
        return
    count = summary.get("availableCount")
    if type(count) is not int or count < 0:
        return
    report.limit_reset_count = count
    report.limit_reset_expirations = []
    if count == 0:
        return
    for credit in summary.get("credits") or []:
        if isinstance(credit, dict) and credit.get("status") == "available":
            expires = parse_reset(credit.get("expiresAt"))
            if expires is not None:
                report.limit_reset_expirations.append(expires)
    report.limit_reset_expirations.sort()


async def live_reports() -> list[Report]:
    """The same live account and usage reads for /whoami and the snapshot timer."""
    return list(await asyncio.gather(asyncio.to_thread(claude_live), codex_live()))
