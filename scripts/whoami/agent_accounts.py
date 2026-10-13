"""Claude and Codex account identity and weekly quota, shared by whoami and agent_notes.

Two kinds of source, and callers pick by cost:

- live: `claude auth status` plus the OAuth usage endpoint, and `codex app-server`
  over stdio. Authoritative, but each call spawns a CLI or hits the network.
- on disk: what the CLIs already wrote for the account logged in right now. Claude
  Code keeps `oauthAccount` and a `cachedUsageUtilization` snapshot in its
  `.claude.json`; Codex keeps an OIDC id_token in `auth.json` whose payload carries
  the email. Free to read, so safe on a two-minute timer.

Each credential store describes its home's current login. Every signed-in Codex
home can be read live; saved usage remains an observation, without credentials.

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
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Sibling scripts and package callers must share the same account identity classes.
if __name__ == "agent_accounts":
    _ = sys.modules.setdefault("scripts.whoami.agent_accounts", sys.modules[__name__])
elif __name__ == "scripts.whoami.agent_accounts":
    _ = sys.modules.setdefault("agent_accounts", sys.modules[__name__])

from scripts.lib.user_zone import user_zone as setting_zone  # noqa: E402


@dataclass(frozen=True)
class KnownCodexAccount:
    home: Path
    login: str
    label: str


@dataclass(frozen=True)
class UnknownCodexAccount:
    home: Path
    reason: str


RefusingCodexAccount = KnownCodexAccount | UnknownCodexAccount


class StoredCodexAccount(TypedDict):
    home: str
    login: str
    label: str


def codex_account(home: Path) -> RefusingCodexAccount:
    from scripts.whoami.note_file import AGENTS_DIR, read_note

    login = codex_email_on_disk(home)
    if not login:
        return UnknownCodexAccount(home, "the home's sign-in cannot be read")
    notes = Path(os.environ.get("AGENT_NOTES_DIR", str(AGENTS_DIR)))
    for path in sorted(notes.glob("*.md")):
        if not path.stem.casefold().startswith("codex"):
            continue
        note = read_note(path)
        if note is not None and (note.get("login") or "").casefold() == login.casefold():
            return KnownCodexAccount(home, login, path.stem)
    return UnknownCodexAccount(home, "the signed-in account has no named note")


def codex_home_role_label(home: Path) -> str:
    """The seats command's label for a default or numbered home."""
    return "default" if home == Path.home() / ".codex" else f"codex {home.name.removeprefix('.codex')}"


ClaudeRole = Literal["showrunners", "unit directors"]


@dataclass(frozen=True)
class KnownClaudeAccount:
    """The login and note label stored for a readable Claude directory."""

    login: str
    label: str


@dataclass(frozen=True)
class UnreadableClaudeAccount:
    """A directory with no readable logged-in Claude identity."""


@dataclass(frozen=True)
class CheckedNamedClaudeAccount:
    """A named directory fit to read, with its resolved login and note label."""

    label: str
    directory: Path
    identity: KnownClaudeAccount


@dataclass(frozen=True)
class ClaudeAccountSelection:
    """One run's role identities and fit named directories, fixed across role changes."""

    default_account: KnownClaudeAccount | UnreadableClaudeAccount
    unit_account: KnownClaudeAccount | UnreadableClaudeAccount
    multiple_accounts: bool
    named_accounts: tuple[CheckedNamedClaudeAccount, ...]

    def is_default_login(self, login: str) -> bool:
        return (
            isinstance(self.default_account, KnownClaudeAccount)
            and login.casefold() == self.default_account.login.casefold()
        )

    def roles_for_login(self, login: str) -> tuple[ClaudeRole, ...]:
        roles: list[ClaudeRole] = []
        if self.is_default_login(login):
            roles.append("showrunners")
        if (
            isinstance(self.unit_account, KnownClaudeAccount)
            and login.casefold() == self.unit_account.login.casefold()
        ):
            roles.append("unit directors")
        return tuple(roles)


WEEK_MINUTES = 7 * 24 * 60
FIVE_HOUR_MINUTES = 5 * 60
EASTERN = ZoneInfo("America/New_York")


def user_zone() -> tzinfo:
    """Use a running production's zone, or the user's zone setting when none runs."""
    try:
        from scripts.production import showrunners

        zone = next((runner["zone"] for runner in showrunners.registered_showrunners()), "")
    except (ImportError, OSError):
        zone = ""
    return ZoneInfo(zone) if zone else setting_zone()


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


@dataclass(frozen=True)
class ClaudeDirectorySource:
    """A Claude reading obtained using this account directory."""

    directory: Path


@dataclass(frozen=True)
class CodexHomeSource:
    """A Codex reading obtained using this account home."""

    home: Path


@dataclass(frozen=True)
class AccountSourceUnspecified:
    """The report does not identify an account directory or home."""


@dataclass(frozen=True)
class NoCodexStartupFailure:
    """No numbered-home startup failure was detected for this report."""


@dataclass(frozen=True)
class CodexStartupFailure:
    """A numbered home did not answer initialize and needs setup."""

    home: Path
    setup_command: str

    @property
    def problem(self) -> str:
        return f"Codex did not start at {codex_home_display(self.home)}; run {self.setup_command} and try again"


CodexStartupStatus = NoCodexStartupFailure | CodexStartupFailure


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
    # The 5-hour window, kept out of `quotas`, which hold the weekly ones.
    five_hour: Quota | None = None
    # Codex credits that carry work past the weekly allowance: whole credits, "unlimited", or "null"
    # when the rate limits were read and report none. None means they were not read.
    credit_balance: str | None = None
    source: ClaudeDirectorySource | CodexHomeSource | AccountSourceUnspecified = field(default_factory=AccountSourceUnspecified)
    codex_startup: CodexStartupStatus = field(default_factory=NoCodexStartupFailure)

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


class CodexCredits(TypedDict, total=False):
    hasCredits: bool
    unlimited: bool
    balance: str | None


class CodexBucket(TypedDict, total=False):
    primary: CodexWindow | None
    secondary: CodexWindow | None
    credits: CodexCredits | None


class CodexRateLimits(TypedDict, total=False):
    rateLimits: CodexBucket
    rateLimitsByLimitId: dict[str, CodexBucket] | None
    rateLimitResetCredits: object


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


def command_json(*args: str, environment: dict[str, str] | None = None) -> object:
    command = (executable(args[0]), *args[1:])
    if environment is None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    else:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, env=environment)
    if result.returncode:
        raise RuntimeError("account command failed")
    return cast(object, json.loads(result.stdout))


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))


def claude_config_file(config_dir: Path | None = None) -> Path:
    """Claude Code's `.claude.json`: inside CLAUDE_CONFIG_DIR when set, else in home."""
    if config_dir is not None:
        return (Path.home() / ".claude.json"
                if config_dir == Path.home() / ".claude"
                else config_dir / ".claude.json")
    if "CLAUDE_CONFIG_DIR" in os.environ:
        return claude_config_dir() / ".claude.json"
    return Path.home() / ".claude.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def seats_home() -> Path:
    """Resolve the shared seats role without using this process's CODEX_HOME."""
    path = Path.home() / ".claude" / "config" / "accounts.json"
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return Path.home() / ".codex"
    if not isinstance(decoded, dict):
        raise ValueError(f"accounts: {path} must contain an object")
    values = cast(dict[str, object], decoded)
    if "seats" not in values:
        return Path.home() / ".codex"
    label = values["seats"]
    if not isinstance(label, str) or re.fullmatch(r"codex [1-9][0-9]*", label) is None:
        raise ValueError(f"accounts: {path} has an invalid seats account")
    return Path.home() / f".codex{label.removeprefix('codex ')}"


def codex_home_display(home: Path) -> str:
    """Display an account home's path using ~ within the user's home directory."""
    user_home = Path.home()
    if home == user_home:
        return "~"
    return f"~/{home.relative_to(user_home)}" if home.is_relative_to(user_home) else str(home)


def codex_homes(seats_home: Path) -> list[Path]:
    """The default home, existing numbered homes, and any other home in use."""
    default = Path.home() / ".codex"
    numbered: list[tuple[int, Path]] = []
    try:
        for child in Path.home().iterdir():
            match = re.fullmatch(r"\.codex([1-9][0-9]*)", child.name)
            if match is not None and child.is_dir():
                numbered.append((int(match.group(1)), child))
    except OSError:
        pass
    homes = [default, *(home for _, home in sorted(numbered))]
    if seats_home not in homes:
        homes.append(seats_home)
    return homes


def codex_report_in_use(report: Report, seats_home: Path) -> bool:
    """Select the seats' home; an older report without a source needs a single home."""
    if report.tool.lower() != "codex":
        return False
    if isinstance(report.source, CodexHomeSource):
        return report.source.home == seats_home
    return isinstance(report.source, AccountSourceUnspecified) and len(codex_homes(seats_home)) == 1


@dataclass(frozen=True)
class CodexWorkAvailable:
    """Both allowance windows have room, or credits can pay for work."""


@dataclass(frozen=True)
class CodexWorkExhausted:
    """At least one current window is used up and no credits remain."""


@dataclass(frozen=True)
class CodexWorkUnknown:
    """The reading cannot establish whether the account can work."""

    problem: str


CodexWorkReadiness = CodexWorkAvailable | CodexWorkExhausted | CodexWorkUnknown


def codex_can_work(report: Report, now: datetime) -> CodexWorkReadiness:
    """Decide from this live reading; an expired window supplies no allowance."""
    if report.problem or report.tool.lower() != "codex":
        return CodexWorkUnknown(report.problem or "not a Codex reading")
    credits = report.credit_balance
    if credits == "unlimited":
        return CodexWorkAvailable()
    try:
        balance = float(credits) if credits is not None else math.nan
    except ValueError:
        balance = math.nan
    if math.isfinite(balance) and balance > 0:
        return CodexWorkAvailable()
    weekly = next((quota for quota in report.quotas if quota.label == "Weekly"), None)
    windows = (report.five_hour, weekly)
    for window in windows:
        if (window is None or window.remaining_percent is None or window.resets_at is None
                or window.resets_at.tzinfo is None or window.resets_at <= now):
            return CodexWorkUnknown("current 5-hour and weekly usage is unavailable; refresh and try again")
    if all(window is not None and (window.remaining_percent or 0) > 0 for window in windows):
        return CodexWorkAvailable()
    if credits == "null" or math.isfinite(balance) and balance == 0:
        return CodexWorkExhausted()
    return CodexWorkUnknown("credit balance is unavailable; refresh and try again")


# --- Claude ------------------------------------------------------------------


def claude_credentials(config_dir: Path | None = None) -> ClaudeCredentials:
    path = (claude_config_dir() if config_dir is None else config_dir) / ".credentials.json"
    if path.exists():
        return cast(ClaudeCredentials, json.loads(path.read_text()))
    # Claude Code uses the macOS login keychain for its default configuration.
    if sys.platform == "darwin" and (
        config_dir == Path.home() / ".claude"
        or (config_dir is None and "CLAUDE_CONFIG_DIR" not in os.environ)
    ):
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


def claude_five_hour(usage: dict[str, object]) -> Quota | None:
    held = usage.get("five_hour")
    if not isinstance(held, dict):
        return None
    window = cast(UsageWindow, cast(object, held))
    return Quota("5-hour", window.get("utilization"), parse_reset(window.get("resets_at")))


def claude_live(config_dir: Path | None = None) -> Report:
    report = Report("Claude", source=ClaudeDirectorySource(
        claude_config_dir() if config_dir is None else config_dir,
    ))
    try:
        if config_dir is None:
            status = command_json("claude", "auth", "status")
        else:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")}
            if config_dir != Path.home() / ".claude":
                environment["CLAUDE_CONFIG_DIR"] = str(config_dir)
            status = command_json("claude", "auth", "status", environment=environment)
        account = cast(ClaudeAuthStatus, status)
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
        token = ((os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") if config_dir is None else None)
                 or (claude_credentials() if config_dir is None else claude_credentials(config_dir))
                 .get("claudeAiOauth", {}).get("accessToken"))
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
        report.five_hour = claude_five_hour(usage)
        if not any(quota.label == "Weekly" for quota in report.quotas):
            report.quota_problem = "Weekly quota: unavailable"
    except urllib.error.HTTPError as error:
        report.quota_problem = f"Weekly quota: unavailable (HTTP {error.code}; try Claude /usage)"
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        report.quota_problem = "Weekly quota: unavailable (could not read live usage; try Claude /usage)"
    return report


def claude_on_disk(config_dir: Path | None = None) -> Report:
    """The logged-in Claude account and the usage Claude Code last cached for it.

    The cache is trusted only when its accountUuid is the logged-in account's, so
    a snapshot left over from before an account switch is never attributed to the
    account that replaced it.
    """
    report = Report("Claude")
    try:
        decoded = cast(object, json.loads(claude_config_file(config_dir).read_text()))
    except (OSError, ValueError):
        report.problem = "Account unavailable (could not read Claude config)"
        return report
    if not isinstance(decoded, dict):
        report.problem = "Account unavailable (could not read Claude config)"
        return report
    config = cast(dict[str, object], cast(object, decoded))
    decoded_account = config.get("oauthAccount")
    if decoded_account is not None and not isinstance(decoded_account, dict):
        report.problem = "Account unavailable (could not read Claude config)"
        return report
    if decoded_account is None:
        report.problem = "Not logged in"
        return report
    account = cast(dict[str, object], cast(object, decoded_account))
    email = account.get("emailAddress")
    if not isinstance(email, str) or not email:
        report.problem = "Not logged in"
        return report
    report.email = email
    decoded_cached = config.get("cachedUsageUtilization")
    if decoded_cached is not None and not isinstance(decoded_cached, dict):
        report.quota_problem = "Weekly quota: cached usage unreadable"
        return report
    cached = (
        cast(dict[str, object], cast(object, decoded_cached))
        if decoded_cached
        else None
    )
    utilization = cached.get("utilization") if cached is not None else None
    if utilization is not None and not isinstance(utilization, dict):
        report.quota_problem = "Weekly quota: cached usage unreadable"
        return report
    if cached is not None and cached.get("accountUuid") == account.get("accountUuid"):
        report.quotas = claude_usage_quotas(
            cast(dict[str, object], cast(object, utilization or {}))
        )
    else:
        report.quota_problem = "Weekly quota: no cached usage for this account"
    return report


# --- Codex -------------------------------------------------------------------


def codex_email_on_disk(home: Path | None = None) -> str | None:
    """The email claim of the id_token in Codex's auth.json, if logged in with ChatGPT."""
    try:
        decoded = cast(
            object,
            json.loads(((home or codex_home()) / "auth.json").read_text()),
        )
    except (OSError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    auth = cast(dict[str, object], cast(object, decoded))
    decoded_tokens = auth.get("tokens")
    if not isinstance(decoded_tokens, dict):
        return None
    tokens = cast(dict[str, object], cast(object, decoded_tokens))
    token = tokens.get("id_token", "")
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded_claims = cast(
            object,
            json.loads(base64.urlsafe_b64decode(payload)),
        )
    except (binascii.Error, ValueError):
        return None
    if not isinstance(decoded_claims, dict):
        return None
    claims = cast(dict[str, object], cast(object, decoded_claims))
    email = claims.get("email")
    return email if isinstance(email, str) and email else None


class CodexServerClosed(RuntimeError):
    """The server closed its output before answering a request."""


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

    async def request(
        self, identifier: int, method: str, params: dict[str, object], *, timeout_seconds: float = 20,
    ) -> object:
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
            raise CodexServerClosed("account API closed")

        return await asyncio.wait_for(receive(), timeout=timeout_seconds)

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


def codex_credits(usage: CodexRateLimits) -> str:
    """The main Codex bucket's credit balance as Report.credit_balance holds it."""
    bucket = (usage.get("rateLimitsByLimitId") or {}).get("codex") or usage.get("rateLimits", {})
    credits = bucket.get("credits")
    if not credits:
        return "null"
    if credits.get("unlimited"):
        return "unlimited"
    try:
        return f"{float(credits.get('balance') or ''):.0f}"
    except ValueError:
        return "null" if credits.get("hasCredits") else "0"


def codex_five_hour(usage: CodexRateLimits) -> Quota | None:
    """The main Codex bucket's 5-hour window."""
    bucket = (usage.get("rateLimitsByLimitId") or {}).get("codex") or usage.get("rateLimits", {})
    for window in (bucket.get("primary"), bucket.get("secondary")):
        if window and window.get("windowDurationMins") == FIVE_HOUR_MINUTES:
            return Quota("5-hour", window.get("usedPercent"), parse_reset(window.get("resetsAt")))
    return None


CODEX_HOME_PREPARATION_SECONDS = 1200.0


@dataclass(frozen=True)
class CodexHomePrepared:
    """The home's server answered initialize after its startup work."""


@dataclass(frozen=True)
class CodexHomePreparationFailed:
    """Startup could not finish within the setup limit."""

    problem: str


CodexHomePreparation = CodexHomePrepared | CodexHomePreparationFailed


async def prepare_codex_home(home: Path) -> CodexHomePreparation:
    """Let Codex finish its own session index, retrying servers that exit unanswered."""
    deadline = time.monotonic() + CODEX_HOME_PREPARATION_SECONDS
    reason = "initialize was not answered"
    while deadline - time.monotonic() > 0:
        server: CodexServer | None = None
        try:
            server = CodexServer(await asyncio.create_subprocess_exec(
                executable("codex"), "app-server", "--listen", "stdio://",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "CODEX_HOME": str(home)},
            ))
            _ = await server.request(
                1, "initialize", {"clientInfo": {"name": "accounts-setup", "version": "1.0"}},
                timeout_seconds=max(0.01, deadline - time.monotonic()),
            )
            await server.send({"method": "initialized"})
            return CodexHomePrepared()
        except asyncio.TimeoutError:
            reason = "initialize was not answered"
            break
        except OSError as error:
            return CodexHomePreparationFailed(f"Codex did not start at {codex_home_display(home)}: {error}")
        except CodexServerClosed as error:
            reason = str(error) or type(error).__name__
        except (RuntimeError, ValueError, KeyError) as error:
            reason = str(error) or type(error).__name__
            break
        finally:
            if server is not None:
                await server.close()
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return CodexHomePreparationFailed(
        f"Codex did not start at {codex_home_display(home)} within {CODEX_HOME_PREPARATION_SECONDS:g}s: "
        + f"{reason}; rerun setup to finish its session index"
    )


async def codex_live(home: Path) -> Report:
    report = Report("Codex", source=CodexHomeSource(home))
    server: CodexServer | None = None
    initialized = False
    try:
        server = CodexServer(await asyncio.create_subprocess_exec(
            executable("codex"), "app-server", "--listen", "stdio://",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "CODEX_HOME": str(home)},
        ))
        _ = await server.request(1, "initialize", {"clientInfo": {"name": "whoami", "version": "1.0"}})
        initialized = True
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
            report.five_hour = codex_five_hour(usage)
            report.credit_balance = codex_credits(usage)
            reset_credits(report, usage.get("rateLimitResetCredits"))
            if not report.quotas:
                report.quota_problem = "Weekly quota: unavailable (no weekly window returned)"
        except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError):
            report.quota_problem = "Weekly quota: unavailable (Codex usage API failed)"
    except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError):
        numbered = re.fullmatch(r"\.codex([1-9][0-9]*)", home.name)
        if not initialized and home.parent == Path.home() and numbered is not None:
            report.codex_startup = CodexStartupFailure(
                home, f'/showrunner:accounts setup "codex {numbered.group(1)}"',
            )
            report.problem = report.codex_startup.problem
        elif report.email is None:
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
    fields = cast(dict[str, object], summary)
    count = fields.get("availableCount")
    if type(count) is not int or count < 0:
        return
    report.limit_reset_count = count
    report.limit_reset_expirations = []
    if count == 0:
        return
    credits = cast(list[object], fields.get("credits") or [])
    for item in credits:
        if isinstance(item, dict):
            credit = cast(dict[str, object], item)
        else:
            continue
        if credit.get("status") == "available":
            expires = parse_reset(credit.get("expiresAt"))
            if expires is not None:
                report.limit_reset_expirations.append(expires)
    report.limit_reset_expirations.sort()


async def live_reports(selection: ClaudeAccountSelection, seats_home: Path) -> list[Report]:
    """Live Claude directories and every Codex home, fetched concurrently."""
    homes = codex_homes(seats_home)
    if not selection.multiple_accounts:
        return list(await asyncio.gather(
            asyncio.to_thread(claude_live), *(codex_live(home) for home in homes),
        ))
    directories = [Path.home() / ".claude", *(named.directory for named in selection.named_accounts)]
    return list(await asyncio.gather(
        *(asyncio.to_thread(claude_live, directory) for directory in directories),
        *(codex_live(home) for home in homes),
    ))
