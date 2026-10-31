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
import hashlib
import json
import os
import math
import re
import shutil
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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
    from scripts.whoami.note_file import read_notes

    login = codex_email_on_disk(home)
    if not login:
        return UnknownCodexAccount(home, "the home's sign-in cannot be read")
    for note in read_notes():
        if note.tool == "codex" and (note.get("login") or "").casefold() == login.casefold():
            return KnownCodexAccount(home, login, note.label)
    return UnknownCodexAccount(home, "the signed-in account has no named note")


@dataclass(frozen=True)
class CreditsOn:
    """This account may pay for work after its allowance is used."""
    warning: str = ""


@dataclass(frozen=True)
class CreditsOff:
    """Only this account's included allowance may pay for work."""


CodexCreditsSetting = CreditsOn | CreditsOff


def codex_credits_setting(label: str) -> CodexCreditsSetting:
    from scripts.whoami.note_file import Note, note_path, read_note

    try:
        path = note_path(label)
    except ValueError:
        # An unnamed home's display label has no account note or explicit setting.
        return CreditsOn()
    note = read_note(path)
    value = note.get("use_credits") if isinstance(note, Note) else None
    if value == "off":
        return CreditsOff()
    if value is None or value == "on":
        return CreditsOn()
    return CreditsOn(f"{label}: use_credits must be on or off; reading it as on")


@dataclass(frozen=True)
class CodexHomeHoldingLogin:
    label: str
    home: Path
    login: str


@dataclass(frozen=True)
class CodexSetupHome:
    """No existing home holds this account's login yet."""
    label: str
    home: Path


def codex_account_home(label: str) -> CodexHomeHoldingLogin | CodexSetupHome:
    from scripts.whoami.note_file import Note, NumberedAccountLabel, parse_account_label, note_path, read_note

    parsed = parse_account_label(label)
    if not isinstance(parsed, NumberedAccountLabel) or parsed.tool != "codex":
        raise ValueError(f"accounts: {label} is not a codexN label")
    label = parsed.label
    note = read_note(note_path(label))
    login = (note.get("login") or "") if isinstance(note, Note) else ""
    if login:
        for home in codex_homes(Path.home() / ".codex"):
            if home.is_symlink() or (home / "auth.json").is_symlink():
                continue
            held = codex_email_on_disk(home)
            if held and held.casefold() == login.casefold():
                return CodexHomeHoldingLogin(label, home, held)
    return CodexSetupHome(label, Path.home() / f".codex{parsed.number}")


def codex_home_role_label(home: Path) -> str:
    """A signed-in account's note number, or its folder when unnamed."""
    identity = codex_account(home)
    return identity.label if isinstance(identity, KnownCodexAccount) else codex_home_display(home)


def codex_home_matches_label(home: Path, label: str) -> bool:
    """Match only a numbered account label to the home holding its login."""
    from scripts.whoami.note_file import NumberedAccountLabel, parse_account_label

    parsed = parse_account_label(label)
    if not isinstance(parsed, NumberedAccountLabel) or parsed.tool != "codex":
        return False
    return (parsed.label == codex_home_role_label(home)
            or codex_account_home(parsed.label).home == home)


ClaudeRole = Literal["showrunners", "unit directors"]


@dataclass(frozen=True)
class NumberedAccount:
    """A readable login with an account note, fit to name a destination."""

    label: str
    home: Path
    login: str


@dataclass(frozen=True)
class UnnotedLogin:
    """A signed-in home whose login has no numbered account note."""

    home: Path
    login: str


@dataclass(frozen=True)
class UnreadableIdentity:
    """A home whose signed-in identity cannot be read."""

    home: Path
    reason: str


AccountIdentity = NumberedAccount | UnnotedLogin | UnreadableIdentity


@dataclass(frozen=True)
class CheckedNamedClaudeAccount:
    """A named directory fit to read, with its numbered identity."""

    label: str
    directory: Path
    identity: NumberedAccount


@dataclass(frozen=True)
class ClaudeAccountsInUse:
    """One run's readable accounts and local sessions' logins."""

    home_directory_account: AccountIdentity
    multiple_accounts: bool
    named_accounts: tuple[CheckedNamedClaudeAccount, ...]
    live_logins: frozenset[str]
    live_directories: tuple[Path, ...] = ()

    def is_home_directory_login(self, login: str) -> bool:
        return (isinstance(self.home_directory_account, (NumberedAccount, UnnotedLogin))
                and login.casefold() == self.home_directory_account.login.casefold())

    def is_in_use(self, login: str) -> bool:
        return self.is_home_directory_login(login) or login.casefold() in self.live_logins


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


@dataclass(frozen=True)
class StaleTokenSkip:
    """The weekly read was skipped because only a Claude session can renew the login."""


@dataclass(frozen=True)
class NoWeeklyReadSkip:
    """The read was not skipped for an expired login token."""


class UsageReadOutcome(str, Enum):
    """The result of the latest attempt to read a login's usage."""

    HELD = "held"
    OK = "ok"
    STALE_TOKEN = "stale_token"
    NO_LOGIN_TOKEN = "no_login_token"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    REFUSED_401 = "refused_401"
    RATE_LIMITED_429 = "rate_limited_429"
    ERROR = "error"


@dataclass(frozen=True)
class UsageReadClaim:
    """This reader owns the login's attempt at this time."""

    at: datetime


@dataclass(frozen=True)
class UsageReadUnclaimed:
    """This report has no stored read claim."""


@dataclass(frozen=True)
class UsageReadHeld:
    """Another recent attempt supplies this login's reading."""


class UsageReadStore(Protocol):
    """The store claims and keeps each live usage attempt."""

    def claim_read(self, login: str, now: datetime) -> UsageReadClaim | UsageReadHeld: ...
    def finish_read(self, login: str, claim: UsageReadClaim, report: Report) -> None: ...


@dataclass(frozen=True)
class KnownUsageLogin:
    """The caller already knows which login this directory holds."""

    login: str


@dataclass(frozen=True)
class UnknownUsageLogin:
    """The caller has no login to use if the identity read fails."""


@dataclass(frozen=True)
class UsageClaimFailed:
    reason: str


@dataclass(frozen=True)
class UsageSaveFailed:
    reason: str


UsageStoreWarning = UsageClaimFailed | UsageSaveFailed


def usage_store_warning_line(warning: UsageStoreWarning) -> str:
    if isinstance(warning, UsageClaimFailed):
        return f"usage store: could not claim the read; {warning.reason}; read without a hold"
    return f"usage store: could not save the read; {warning.reason}; keep the live report"


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
    weekly_read_skip: StaleTokenSkip | NoWeeklyReadSkip = field(default_factory=NoWeeklyReadSkip)
    usage_read_outcome: UsageReadOutcome = UsageReadOutcome.ERROR
    usage_read_claim: UsageReadClaim | UsageReadUnclaimed = field(default_factory=UsageReadUnclaimed)
    last_known_login: KnownUsageLogin | UnknownUsageLogin = field(default_factory=UnknownUsageLogin)
    store_warnings: tuple[UsageStoreWarning, ...] = ()

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
    subscriptionType: str
    # Epoch milliseconds.
    expiresAt: int


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
    # Claude reports a signed-out session as JSON with exit 1.
    if result.returncode and (result.returncode != 1 or args != ("claude", "auth", "status")):
        raise RuntimeError("account command failed")
    decoded = cast(object, json.loads(result.stdout))
    if result.returncode and not (isinstance(decoded, dict)
            and cast(dict[str, object], decoded).get("loggedIn") is False):
        raise RuntimeError("account command failed")
    return decoded


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))


def claude_config_value(folder: Path) -> str:
    """The exact directory value passed to Claude Code when selecting a folder."""
    return str(folder)


def claude_keychain_service(config_dir: Path | None = None) -> str:
    """Claude Code's service for the selected folder, without reading its entry."""
    if config_dir is None:
        value = os.environ.get("CLAUDE_CONFIG_DIR")
        if not value:
            return "Claude Code-credentials"
    elif config_dir == Path.home() / ".claude":
        return "Claude Code-credentials"
    else:
        value = claude_config_value(config_dir)
    digest = hashlib.sha256(unicodedata.normalize("NFC", value).encode("utf-8")).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


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


def workers_home() -> Path:
    """Resolve only the authoritative workers account, never the caller's home."""
    from scripts.accounts.role_map import read_role_map, RoleAccountUnavailable

    label = read_role_map().workers
    if not label:
        raise RoleAccountUnavailable("accounts: workers has no mapped account; run accounts.py set-role workers <codexN>")
    if label == "default":
        # Read old explicit selections without inventing a missing selection.
        label = codex_home_role_label(Path.home() / ".codex")
    try:
        selected = codex_account_home(label)
    except ValueError as error:
        raise RoleAccountUnavailable(str(error)) from error
    if isinstance(selected, CodexHomeHoldingLogin):
        return selected.home
    state = "not logged in" if selected.home.is_dir() else "not set up"
    step = (f"CODEX_HOME={shlex.quote(str(selected.home))} codex login" if selected.home.is_dir()
            else f"run accounts.py setup {label}")
    raise RoleAccountUnavailable(f"accounts: workers is on {label}, which is {state} on this machine; {step}")


@dataclass(frozen=True)
class NoWorkersHome:
    reason: str


def observe_workers_home() -> Path | NoWorkersHome:
    """Observe a mapped home even when signed out; never choose a replacement."""
    from scripts.accounts.role_map import read_role_map

    try:
        label = read_role_map().workers
        if not label:
            return NoWorkersHome("accounts: workers has no mapped account; run accounts.py set-role workers <codexN>")
        if label == "default":
            label = codex_home_role_label(Path.home() / ".codex")
        return codex_account_home(label).home
    except ValueError as error:
        return NoWorkersHome(str(error))


def codex_home_display(home: Path) -> str:
    """Display an account home's path using ~ within the user's home directory."""
    user_home = Path.home()
    if home == user_home:
        return "~"
    return f"~/{home.relative_to(user_home)}" if home.is_relative_to(user_home) else str(home)


def codex_homes(workers_home: Path | NoWorkersHome) -> list[Path]:
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
    if isinstance(workers_home, Path) and workers_home not in homes:
        homes.append(workers_home)
    return homes


def codex_report_in_use(report: Report, workers_home: Path | NoWorkersHome) -> bool:
    """Select the workers' home; an older report without a source needs a single home."""
    if report.tool.lower() != "codex" or isinstance(workers_home, NoWorkersHome):
        return False
    if isinstance(report.source, CodexHomeSource):
        return report.source.home == workers_home
    return isinstance(report.source, AccountSourceUnspecified) and len(codex_homes(workers_home)) == 1


@dataclass(frozen=True)
class CodexWorkAvailable:
    """Every reported allowance window has room, or credits can pay for work."""


@dataclass(frozen=True)
class CodexWorkExhausted:
    """At least one current window is used up and no credits remain."""


@dataclass(frozen=True)
class CodexWorkUnknown:
    """The reading cannot establish whether the account can work."""

    problem: str


CodexWorkReadiness = CodexWorkAvailable | CodexWorkExhausted | CodexWorkUnknown


def codex_can_work(report: Report, now: datetime, credits: CodexCreditsSetting) -> CodexWorkReadiness:
    """Decide from this live reading; an expired window supplies no allowance."""
    if report.problem or report.tool.lower() != "codex":
        return CodexWorkUnknown(report.problem or "not a Codex reading")
    balance_text = report.credit_balance
    if isinstance(credits, CreditsOn) and balance_text == "unlimited":
        return CodexWorkAvailable()
    try:
        balance = float(balance_text) if balance_text is not None else math.nan
    except ValueError:
        balance = math.nan
    if isinstance(credits, CreditsOn) and math.isfinite(balance) and balance > 0:
        return CodexWorkAvailable()
    weekly = next((quota for quota in report.quotas if quota.label == "Weekly"), None)
    # Codex returns a 5-hour window only once the account has used it, so a
    # missing 5-hour window means no use in it yet: all of it is left.
    windows = (weekly,) if report.five_hour is None else (weekly, report.five_hour)
    for window in windows:
        if (window is None or window.remaining_percent is None
                or window.resets_at is not None and (window.resets_at.tzinfo is None or window.resets_at <= now)
                or window.resets_at is None and window is not weekly):
            return CodexWorkUnknown("current 5-hour and weekly usage is unavailable; refresh and try again")
    if all(window is not None and (window.remaining_percent or 0) > 0 for window in windows):
        return CodexWorkAvailable()
    if isinstance(credits, CreditsOff) or balance_text == "null" or math.isfinite(balance) and balance == 0:
        return CodexWorkExhausted()
    return CodexWorkUnknown("credit balance is unavailable; refresh and try again")


# --- Claude ------------------------------------------------------------------


class ClaudeCredentialsUnavailable(RuntimeError):
    """This process cannot read the account credentials in its session."""


def claude_credentials(config_dir: Path | None = None) -> ClaudeCredentials:
    path = (claude_config_dir() if config_dir is None else config_dir) / ".credentials.json"
    try:
        if path.exists():
            return cast(ClaudeCredentials, json.loads(path.read_text()))
    except OSError as error:
        raise ClaudeCredentialsUnavailable("usage credentials unavailable") from error
    if sys.platform == "darwin":
        try:
            return cast(ClaudeCredentials, command_json(
                "security", "find-generic-password", "-s", claude_keychain_service(config_dir), "-w"))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            raise ClaudeCredentialsUnavailable("usage credentials unavailable") from error
    raise ClaudeCredentialsUnavailable("usage credentials unavailable")


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


def claude_live(config_dir: Path | None = None, *, use_disk_identity: bool = False) -> Report:
    store = cast(UsageReadStore, cast(object, import_module("scripts.whoami.usage_store")))

    report = Report("Claude", source=ClaudeDirectorySource(
        claude_config_dir() if config_dir is None else config_dir,
    ))
    stored_credentials: ClaudeCredentials = {}
    try:
        disk = claude_on_disk(config_dir) if use_disk_identity and config_dir is not None else Report("Claude")
        if disk.email:
            try:
                stored_credentials = claude_credentials(config_dir)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                pass
        subscription = stored_credentials.get("claudeAiOauth", {}).get("subscriptionType")
        if disk.email and isinstance(subscription, str) and subscription:
            # The usage endpoint checks the stored token. Do not start a CLI to read this login again.
            status: object = {"loggedIn": True, "email": disk.email, "authMethod": "claude.ai",
                              "subscriptionType": subscription}
        elif config_dir is None:
            status = command_json("claude", "auth", "status")
        else:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")}
            if config_dir != Path.home() / ".claude":
                environment["CLAUDE_CONFIG_DIR"] = claude_config_value(config_dir)
            status = command_json("claude", "auth", "status", environment=environment)
        account = cast(ClaudeAuthStatus, status)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        report.problem = "Account unavailable (claude auth status failed)"
        return report
    if not account.get("loggedIn"):
        report.usage_read_outcome = UsageReadOutcome.CREDENTIALS_UNAVAILABLE
        report.problem = "Login credentials unavailable in this session"
        return report
    report.email = account.get("email")
    report.plan = account.get("subscriptionType") or account.get("authMethod")
    if account.get("authMethod") != "claude.ai":
        report.quota_problem = "Weekly quota: unavailable for this authentication method"
        return report
    claim: UsageReadClaim | UsageReadHeld = UsageReadHeld()
    try:
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") if config_dir is None else None
        if not token:
            if not stored_credentials:
                stored_credentials = claude_credentials() if config_dir is None else claude_credentials(config_dir)
            oauth = stored_credentials.get("claudeAiOauth", {})
            token = oauth.get("accessToken")
            expires = oauth.get("expiresAt")
            # Only a Claude session refreshes a stored token, so an idle account's goes stale.
            # The service answers a stale one with 429 or 401; asking with it only adds refused calls.
            if token and isinstance(expires, int) and expires / 1000 <= time.time():
                report.weekly_read_skip = StaleTokenSkip()
                report.usage_read_outcome = UsageReadOutcome.STALE_TOKEN
                report.quota_problem = "Weekly quota: not read (stale token; a Claude session on this account refreshes it)"
                return report
        if not token:
            report.usage_read_outcome = UsageReadOutcome.NO_LOGIN_TOKEN
            report.quota_problem = "Weekly quota: not read (no login token; start a Claude session on this account)"
            return report
        request = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        if not report.email:
            raise RuntimeError("usage login unavailable")
        try:
            claim = store.claim_read(report.email, datetime.now(timezone.utc))
        except (OSError, ValueError) as error:
            report.store_warnings += (UsageClaimFailed(str(error)),)
            print(usage_store_warning_line(report.store_warnings[-1]), file=sys.stderr)
        else:
            if isinstance(claim, UsageReadHeld):
                report.usage_read_outcome = UsageReadOutcome.HELD
                return report
            report.usage_read_claim = claim
        opened = cast(http.client.HTTPResponse, urllib.request.urlopen(request, timeout=15))
        with opened as response:
            usage = cast(dict[str, object], json.loads(response.read()))
        report.quotas = claude_usage_quotas(usage)
        report.five_hour = claude_five_hour(usage)
        if any(quota.label == "Weekly" and quota.remaining_percent is not None for quota in report.quotas):
            report.usage_read_outcome = UsageReadOutcome.OK
        if not any(quota.label == "Weekly" for quota in report.quotas):
            report.quota_problem = "Weekly quota: unavailable"
    except ClaudeCredentialsUnavailable:
        report.usage_read_outcome = UsageReadOutcome.CREDENTIALS_UNAVAILABLE
        report.quota_problem = "Weekly quota: not read (login credentials unavailable in this session)"
    except urllib.error.HTTPError as error:
        report.usage_read_outcome = (UsageReadOutcome.REFUSED_401 if error.code == 401 else
                                     UsageReadOutcome.RATE_LIMITED_429 if error.code == 429 else UsageReadOutcome.ERROR)
        report.quota_problem = f"Weekly quota: unavailable (HTTP {error.code}; try Claude /usage)"
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        report.quota_problem = "Weekly quota: unavailable (could not read live usage; try Claude /usage)"
    finally:
        if report.email and isinstance(claim, UsageReadClaim):
            try:
                store.finish_read(report.email, claim, report)
            except (OSError, ValueError) as error:
                report.store_warnings += (UsageSaveFailed(str(error)),)
                print(usage_store_warning_line(report.store_warnings[-1]), file=sys.stderr)
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
            if any(quota.label == "Weekly" and quota.remaining_percent is not None for quota in report.quotas):
                report.usage_read_outcome = UsageReadOutcome.OK
            if not report.quotas:
                report.quota_problem = "Weekly quota: unavailable (no weekly window returned)"
        except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError):
            report.quota_problem = "Weekly quota: unavailable (Codex usage API failed)"
    except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError):
        numbered = re.fullmatch(r"\.codex([1-9][0-9]*)", home.name)
        if not initialized and home.parent == Path.home() and numbered is not None:
            report.codex_startup = CodexStartupFailure(
                home, f"/showrunner:accounts setup codex{numbered.group(1)}",
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


async def live_reports(selection: ClaudeAccountsInUse, workers_home: Path | NoWorkersHome) -> list[Report]:
    """Live Claude directories and every Codex home, fetched concurrently."""
    homes = codex_homes(workers_home)
    directories = list(dict.fromkeys([
        *(named.directory for named in selection.named_accounts),
        *selection.live_directories,
    ]))
    home_login = claude_on_disk(Path.home() / ".claude").email
    if home_login and Path.home() / ".claude" not in directories:
        directories.append(Path.home() / ".claude")
    reports = list(await asyncio.gather(
        *(asyncio.to_thread(claude_live, directory, use_disk_identity=True) for directory in directories),
        *(codex_live(home) for home in homes),
    ))
    known = {named.directory: named.identity.login for named in selection.named_accounts}
    if home_login:
        known[Path.home() / ".claude"] = home_login
    for report in reports:
        if isinstance(report.source, ClaudeDirectorySource) and report.source.directory in known:
            report.last_known_login = KnownUsageLogin(known[report.source.directory])
    return reports
