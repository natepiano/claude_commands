"""Journal one bounded Hana lifecycle step under the shared controller lock."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
import json
from pathlib import Path
import time
from typing import Protocol, cast
from uuid import uuid4

from scripts.aws_target import hana_guest, hana_spend, ledger
from scripts.aws_target.controller_lock import control_lock
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic


class Lifecycle(StrEnum):
    UNPROVISIONED = 'unprovisioned'
    IDLE_STOPPED = 'idle-stopped'
    INTERRUPTED = 'interrupted'
    STOPPED_UNKNOWN = 'stopped-unknown'
    STARTING = 'starting'
    READY = 'ready'
    STOPPING = 'stopping'
    REPLACEMENT_PENDING = 'replacement-pending'


class StopCause(StrEnum):
    IDLE = 'idle'
    INTERRUPTION = 'interruption'
    UNKNOWN = 'unknown'


class Action(StrEnum):
    OBSERVE = 'observe'
    BOOTSTRAP = 'bootstrap'
    START = 'start'
    STOP = 'stop'
    PUBLISH = 'publish'


@dataclass(frozen=True)
class LifecycleSettings:
    idle_stop_seconds: int
    start_timeout_seconds: int
    tick_interval_s: int
    controller_tick_timeout_s: int


def read_settings(configuration: conf.ConfigRead) -> LifecycleSettings:
    settings = LifecycleSettings(*(configuration.whole(key) for key in
        ('idle_stop_seconds', 'start_timeout_seconds', 'tick_interval_s', 'controller_tick_timeout_s')))
    safety = hana_guest.read_settings(configuration)
    if (min(settings.idle_stop_seconds, settings.start_timeout_seconds, settings.tick_interval_s,
            settings.controller_tick_timeout_s) <= 0
            or settings.tick_interval_s + settings.controller_tick_timeout_s >= safety.guard_grace_seconds
            or settings.start_timeout_seconds + 3 * settings.controller_tick_timeout_s >= safety.permit_max_seconds):
        raise ValueError('Hana lifecycle intervals exceed the guest safety windows')
    return settings


@dataclass(frozen=True)
class DeploymentIdentity:
    deployment: str
    instance: str
    spot_request: str
    region: str


@dataclass(frozen=True)
class NoDeployment:
    """Setup has not registered an approved Hana deployment."""


@dataclass(frozen=True)
class VerifiedBoot:
    """The provider verified this root receiver response against the demand-start challenge."""

    boot_id: str
    start_token: str


@dataclass(frozen=True)
class BootUnverified:
    """No current root receiver response establishes the guest boot identity."""


@dataclass(frozen=True)
class WorkClaim:
    execution: str
    token: str
    requested_at: int
    active: bool = False


@dataclass(frozen=True)
class DemandStart:
    token: str
    at: int
    deadline: int


@dataclass(frozen=True)
class NoDemandStart:
    """No demand start owns the current boot."""


@dataclass(frozen=True)
class DeliveredPermit:
    generation: int
    expires: int
    execution: str


@dataclass(frozen=True)
class NoDeliveredPermit:
    """The guest has not acknowledged a permit for the current claim."""


@dataclass(frozen=True)
class PendingOperation:
    action: Action
    token: str
    at: int
    generation: int
    permit: hana_guest.WorkPermit | hana_guest.NoWorkPermit = hana_guest.NoWorkPermit()
    expected_generation: int = 0
    execution: str = ''


@dataclass(frozen=True)
class NoOperation:
    """No provider response remains unresolved."""


@dataclass
class BoxRecord:
    identity: DeploymentIdentity
    state: Lifecycle
    generation: int = 0
    claims: list[WorkClaim] = field(default_factory=list)
    start: DemandStart | NoDemandStart = NoDemandStart()
    boot: VerifiedBoot | BootUnverified = BootUnverified()
    delivered: DeliveredPermit | NoDeliveredPermit = NoDeliveredPermit()
    operation: PendingOperation | NoOperation = NoOperation()
    idle_since: int = 0
    observed_at: int = 0
    stop_cause: StopCause = StopCause.UNKNOWN
    reason: str = ''


@dataclass(frozen=True)
class BoxObservation:
    identity: DeploymentIdentity
    at: int
    state: ledger.InstanceState
    boot: VerifiedBoot | BootUnverified = BootUnverified()
    stop_cause: StopCause = StopCause.UNKNOWN


@dataclass(frozen=True)
class OperationAccepted:
    token: str


@dataclass(frozen=True)
class PermitAccepted:
    token: str
    identity: DeploymentIdentity
    boot: VerifiedBoot
    generation: int
    execution: str


@dataclass(frozen=True)
class MutationUnresolved:
    """The original mutation can still complete after this observation."""

    token: str
    observed: BoxObservation


@dataclass(frozen=True)
class MutationSettled:
    """The provider confirms that this mutation cannot produce further changes."""

    token: str
    observed: BoxObservation


MutationRecovery = MutationUnresolved | MutationSettled


ProviderReply = BoxObservation | OperationAccepted | PermitAccepted
NO_WORK_PERMIT = hana_guest.NoWorkPermit()


class HanaProvider(Protocol):
    def step(self, identity: DeploymentIdentity, start: DemandStart | NoDemandStart,
             operation: PendingOperation) -> ProviderReply:
        """Run one bounded step; require accepted spending before bootstrap, start, or publication."""
        ...

    def recover(self, identity: DeploymentIdentity, start: DemandStart | NoDemandStart,
                operation: PendingOperation) -> MutationRecovery:
        """Read the mutation outcome without starting, bootstrapping, or publishing a permit."""
        ...


class ProviderNotInstalled:
    """D2 installs the concrete AWS and root receiver transport after approved setup."""

    def step(self, identity: DeploymentIdentity, start: DemandStart | NoDemandStart,
             operation: PendingOperation) -> ProviderReply:
        _ = identity, start, operation
        raise RuntimeError('The Hana provider transport is not installed; preserve the pending operation')


    def recover(self, identity: DeploymentIdentity, start: DemandStart | NoDemandStart,
                operation: PendingOperation) -> MutationRecovery:
        _ = identity, start, operation
        raise RuntimeError('The Hana provider transport is not installed; preserve the pending operation')


@dataclass(frozen=True)
class ClaimCommitted:
    claim: WorkClaim


@dataclass(frozen=True)
class ClaimRefused:
    reason: str


@dataclass(frozen=True)
class ClaimWaiting:
    claim: WorkClaim
    timed_out: bool


@dataclass(frozen=True)
class CommandPermission:
    execution: str
    boot: VerifiedBoot
    generation: int
    expires: int


def _record(row: BoxRecord) -> dict[str, object]:
    operation = row.operation
    permit = operation.permit if isinstance(operation, PendingOperation) else hana_guest.NoWorkPermit()
    return {
        'version': 1, 'identity': dict(deployment=row.identity.deployment, instance=row.identity.instance,
            spot_request=row.identity.spot_request, region=row.identity.region), 'state': row.state.value,
        'generation': row.generation, 'claims': [dict(execution=c.execution, token=c.token,
            requested_at=c.requested_at, active=c.active) for c in row.claims],
        'start': dict(token=row.start.token, at=row.start.at, deadline=row.start.deadline)
            if isinstance(row.start, DemandStart) else None,
        'boot': dict(boot_id=row.boot.boot_id, start_token=row.boot.start_token) if isinstance(row.boot, VerifiedBoot) else None,
        'delivered': dict(generation=row.delivered.generation, expires=row.delivered.expires, execution=row.delivered.execution)
            if isinstance(row.delivered, DeliveredPermit) else None,
        'operation': dict(action=operation.action.value, token=operation.token, at=operation.at,
            generation=operation.generation, expected_generation=operation.expected_generation, execution=operation.execution,
            permit=dict(expires_at=int(permit.expires_at), generation=permit.generation, boot_id=permit.boot_id)
                if isinstance(permit, hana_guest.WorkPermit) else None) if isinstance(operation, PendingOperation) else None,
        'idle_since': row.idle_since, 'observed_at': row.observed_at, 'stop_cause': row.stop_cause.value, 'reason': row.reason,
    }


def _boot_is_current(row: BoxRecord, boot: VerifiedBoot | BootUnverified) -> bool:
    return (isinstance(boot, VerifiedBoot) and bool(boot.boot_id)
        and isinstance(row.start, DemandStart) and boot.start_token == row.start.token
        and (isinstance(row.boot, BootUnverified) or row.boot == boot))


def _empty_text(document: dict[str, object], key: str) -> str:
    value = document[key]
    if not isinstance(value, str):
        raise ValueError(key + ': expected text')
    return value


def _decode(document: dict[str, object]) -> BoxRecord:
    if document.get('version') != 1:
        raise ValueError('The Hana lifecycle journal version is unknown')
    identity = ledger.mapping(document['identity'])
    names = tuple(ledger.text(identity, key) for key in ('deployment', 'instance', 'spot_request', 'region'))
    if not all(names):
        raise ValueError('The Hana deployment identity is incomplete')
    row = BoxRecord(DeploymentIdentity(*names), Lifecycle(ledger.text(document, 'state')),
        ledger.instant(document, 'generation'),
        [WorkClaim(ledger.text(c, 'execution'), ledger.text(c, 'token'), ledger.instant(c, 'requested_at'), c['active'] is True)
         for c in ledger.rows(document, 'claims')], idle_since=ledger.instant(document, 'idle_since'),
        observed_at=ledger.instant(document, 'observed_at'), stop_cause=StopCause(ledger.text(document, 'stop_cause')),
        reason=_empty_text(document, 'reason'))
    if len(row.claims) > 1 or any(not c.execution or not c.token for c in row.claims):
        raise ValueError('The Hana claim record is invalid')
    if document['start'] is not None:
        item = ledger.mapping(document['start'])
        row.start = DemandStart(ledger.text(item, 'token'), ledger.instant(item, 'at'), ledger.instant(item, 'deadline'))
    if document['boot'] is not None:
        item = ledger.mapping(document['boot'])
        row.boot = VerifiedBoot(ledger.text(item, 'boot_id'), ledger.text(item, 'start_token'))
    if document['delivered'] is not None:
        item = ledger.mapping(document['delivered'])
        row.delivered = DeliveredPermit(ledger.instant(item, 'generation'), ledger.instant(item, 'expires'), ledger.text(item, 'execution'))
    if document['operation'] is not None:
        item = ledger.mapping(document['operation'])
        permit: hana_guest.WorkPermit | hana_guest.NoWorkPermit = hana_guest.NoWorkPermit()
        if item['permit'] is not None:
            raw = ledger.mapping(item['permit'])
            permit = hana_guest.WorkPermit(float(ledger.instant(raw, 'expires_at')), ledger.text(raw, 'generation'), ledger.text(raw, 'boot_id'))
        row.operation = PendingOperation(Action(ledger.text(item, 'action')), ledger.text(item, 'token'), ledger.instant(item, 'at'),
            ledger.instant(item, 'generation'), permit, ledger.instant(item, 'expected_generation'), _empty_text(item, 'execution'))
    return row


class HanaController:
    """Serialize claims and durable intents before releasing the lock for provider calls."""

    def __init__(self, directory: Path, settings_path: Path, workload_path: Path, provider: HanaProvider,
                 *, clock: Callable[[], float] = time.time):
        self.directory: Path = directory
        self.path: Path = directory / 'hana' / 'lifecycle.json'
        self.settings_path: Path = settings_path
        self.workload_path: Path = workload_path
        self.provider: HanaProvider = provider
        self.clock: Callable[[], float] = clock

    def _read(self) -> BoxRecord | NoDeployment:
        try:
            return _decode(ledger.mapping(cast(object, json.loads(self.path.read_text()))))
        except FileNotFoundError:
            return NoDeployment()

    def _save(self, row: BoxRecord) -> None:
        write_text_atomic(self.path, json.dumps(_record(row), sort_keys=True) + '\n')

    def _spending(self) -> hana_spend.HanaSpending:
        return hana_spend.HanaSpending(ledger.CostLedger(self.directory), self.settings_path,
            hana_spend.read_settings(conf.read(self.workload_path)))

    def register(self, identity: DeploymentIdentity, *, state: Lifecycle = Lifecycle.IDLE_STOPPED) -> None:
        """Register D2's approved identities without provisioning or enabling placement."""
        with control_lock(self.directory):
            previous = self._read()
            if not isinstance(previous, NoDeployment):
                if previous.identity != identity:
                    raise ValueError('An existing deployment needs explicit replacement')
                return
            if not all((identity.deployment, identity.instance, identity.spot_request, identity.region)):
                raise ValueError('The Hana deployment identity is incomplete')
            self._save(BoxRecord(identity, state, idle_since=int(self.clock())))

    def request(self, execution: str) -> ClaimCommitted | ClaimRefused:
        """Commit incoming work under the same lock that selects an idle stop."""
        if not execution:
            return ClaimRefused('The execution identity is missing')
        with control_lock(self.directory):
            row = self._read()
            if isinstance(row, NoDeployment):
                return ClaimRefused('Hana is unprovisioned')
            if not self._spending().settings.enabled:
                return ClaimRefused('Hana is disabled')
            if row.state not in (Lifecycle.IDLE_STOPPED, Lifecycle.STARTING, Lifecycle.READY):
                return ClaimRefused('Hana recovery must settle before another claim')
            if isinstance(row.operation, PendingOperation) and row.operation.action == Action.STOP:
                return ClaimRefused('The idle stop already owns the machine')
            if row.claims:
                return ClaimCommitted(row.claims[0]) if row.claims[0].execution == execution else ClaimRefused('Hana is busy')
            claim = WorkClaim(execution, uuid4().hex, int(self.clock()))
            row.claims = [claim]
            row.reason = ''
            self._save(row)
            return ClaimCommitted(claim)

    def claim_status(self, execution: str) -> ClaimWaiting | ClaimRefused:
        with control_lock(self.directory):
            row = self._read()
            if isinstance(row, NoDeployment) or not row.claims or row.claims[0].execution != execution:
                return ClaimRefused(row.reason if isinstance(row, BoxRecord) and row.reason else 'No current claim owns this execution')
            settings = read_settings(conf.read(self.workload_path))
            claim = row.claims[0]
            return ClaimWaiting(claim, int(self.clock()) >= claim.requested_at + settings.start_timeout_seconds)

    def release(self, execution: str) -> bool:
        """Keep unresolved starts owned until lifecycle recovery settles them."""
        with control_lock(self.directory):
            row = self._read()
            if isinstance(row, NoDeployment) or not row.claims or row.claims[0].execution != execution:
                return False
            if row.state in (Lifecycle.STARTING, Lifecycle.STOPPING) or isinstance(row.operation, PendingOperation):
                return False
            row.claims = []
            row.delivered = NoDeliveredPermit()
            row.idle_since = int(self.clock())
            self._save(row)
            return True

    def permission(self, execution: str, boot_id: str, generation: int) -> CommandPermission | ClaimRefused:
        with control_lock(self.directory):
            row = self._read()
            now = int(self.clock())
            if (isinstance(row, NoDeployment) or row.state != Lifecycle.READY or not row.claims
                    or not row.claims[0].active or row.claims[0].execution != execution
                    or not isinstance(row.boot, VerifiedBoot) or row.boot.boot_id != boot_id
                    or not isinstance(row.delivered, DeliveredPermit) or row.delivered.execution != execution
                    or row.delivered.generation != generation or row.generation != generation or now >= row.delivered.expires):
                return ClaimRefused('The claim, boot identity, or delivered permit is stale')
            spending = self._spending().bind_start(now, generation, boot_id)
            if isinstance(spending, hana_spend.PermitRefused):
                return ClaimRefused(spending.reason)
            return CommandPermission(execution, row.boot, generation, row.delivered.expires)

    def _intent(self, row: BoxRecord, action: Action, now: int, *, permit: hana_guest.WorkPermit | hana_guest.NoWorkPermit = NO_WORK_PERMIT,
                expected_generation: int = 0) -> PendingOperation:
        operation = PendingOperation(action, uuid4().hex, now, row.generation, permit, expected_generation,
            row.claims[0].execution if row.claims else '')
        row.operation = operation
        self._save(row)
        return operation

    def tick(self) -> Lifecycle:
        """Run one provider step without holding the controller or ledger lock during network work."""
        now = int(self.clock())
        with control_lock(self.directory):
            row = self._read()
            if isinstance(row, NoDeployment):
                return Lifecycle.UNPROVISIONED
            settings = read_settings(conf.read(self.workload_path))
            spending = self._spending()
            if isinstance(row.operation, NoOperation):
                if row.state == Lifecycle.IDLE_STOPPED and row.claims and spending.settings.enabled:
                    reserved = spending.reserve_start(now, now + spending.settings.permit_max_seconds)
                    if isinstance(reserved, hana_spend.PermitRefused):
                        row.reason, row.claims = reserved.reason, []
                        self._save(row)
                        return row.state
                    row.generation = reserved.exposure.generation
                    row.start = DemandStart(uuid4().hex, now, now + settings.start_timeout_seconds)
                    row.boot, row.delivered = BootUnverified(), NoDeliveredPermit()
                    row.state, row.stop_cause = Lifecycle.STARTING, StopCause.UNKNOWN
                    _ = self._intent(row, Action.BOOTSTRAP, now)
                else:
                    _ = self._intent(row, Action.OBSERVE, now)
            operation = row.operation
            assert isinstance(operation, PendingOperation)
            recover = False
            if operation.action in (Action.BOOTSTRAP, Action.START, Action.PUBLISH):
                allowed = spending.check_start(now, operation.generation)
                if isinstance(allowed, hana_spend.PermitRefused):
                    row.reason = allowed.reason
                    self._save(row)
                    recover = True
            identity, start = row.identity, row.start
        reply = (self.provider.recover(identity, start, operation) if recover
                 else self.provider.step(identity, start, operation))
        with control_lock(self.directory):
            current = self._read()
            if (isinstance(current, NoDeployment) or current.identity != identity
                    or current.operation != operation):
                return current.state if isinstance(current, BoxRecord) else Lifecycle.UNPROVISIONED
            row = current
            now = int(self.clock())
            if isinstance(reply, (MutationUnresolved, MutationSettled)):
                self._recover(row, operation, reply, settings, now)
            elif isinstance(reply, BoxObservation):
                if operation.action != Action.OBSERVE or reply.identity != row.identity or not operation.at <= reply.at <= now:
                    raise ValueError('The provider observation has a stale identity or time')
                self._observe(row, reply, settings, now)
            elif isinstance(reply, OperationAccepted):
                if reply.token != operation.token or operation.action not in (Action.BOOTSTRAP, Action.START, Action.STOP):
                    raise ValueError('The provider accepted a different operation')
                row.operation = NoOperation()
                if operation.action == Action.BOOTSTRAP:
                    _ = self._intent(row, Action.START, now)
                else:
                    _ = self._intent(row, Action.OBSERVE, now)
            else:
                self._accept_permit(row, operation, reply, now)
            self._save(row)
            return row.state

    def _recover(self, row: BoxRecord, operation: PendingOperation, reply: MutationRecovery,
                 settings: LifecycleSettings, now: int) -> None:
        observed = reply.observed
        if (reply.token != operation.token or observed.identity != row.identity
                or not max(operation.at, row.observed_at) <= observed.at <= now):
            raise ValueError('The mutation recovery has a stale operation, identity, or time')
        active = observed.state in ('pending', 'running', 'stopping', 'shutting-down')
        if active:
            spending = self._spending()
            spending.reconcile(observed.at, observed.state, generation=row.generation)
            row.generation = max((r.generation for r in spending.book.read_snapshot().hana_compute), default=0)
            row.observed_at = observed.at
        if isinstance(reply, MutationUnresolved):
            # A stopped observation cannot close exposure while the original mutation can still complete.
            return
        if active:
            valid_boot = _boot_is_current(row, observed.boot)
            self._stop(row, now, StopCause.IDLE if valid_boot else StopCause.UNKNOWN)
        else:
            self._observe(row, observed, settings, now)

    def _stop(self, row: BoxRecord, now: int, cause: StopCause) -> None:
        row.state, row.stop_cause, row.delivered = Lifecycle.STOPPING, cause, NoDeliveredPermit()
        _ = self._intent(row, Action.STOP, now)

    def _observe(self, row: BoxRecord, observed: BoxObservation, settings: LifecycleSettings, now: int) -> None:
        if observed.at < row.observed_at:
            return
        spending = self._spending()
        spending.reconcile(observed.at, observed.state, generation=row.generation)
        snapshot = spending.book.read_snapshot()
        row.generation = max((r.generation for r in snapshot.hana_compute), default=0)
        row.observed_at, row.operation = observed.at, NoOperation()
        if observed.state in ('stopped', 'terminated'):
            stopped = not any(isinstance(r.state, ledger.HanaExposureOpen) for r in snapshot.hana_compute)
            if not stopped:
                return
            row.delivered, row.boot, row.start = NoDeliveredPermit(), BootUnverified(), NoDemandStart()
            if observed.state == 'terminated':
                row.state = Lifecycle.REPLACEMENT_PENDING
            elif observed.stop_cause == StopCause.INTERRUPTION:
                row.state = Lifecycle.INTERRUPTED
            elif row.state == Lifecycle.STOPPING and row.stop_cause == StopCause.IDLE:
                row.state = Lifecycle.IDLE_STOPPED
            elif row.state != Lifecycle.IDLE_STOPPED:
                row.state = Lifecycle.STOPPED_UNKNOWN
            return
        if observed.state != 'running':
            return
        valid_boot = _boot_is_current(row, observed.boot)
        if not spending.settings.enabled or not row.claims or not valid_boot:
            if row.state == Lifecycle.READY and not row.claims and valid_boot and spending.settings.enabled:
                if now - row.idle_since < settings.idle_stop_seconds:
                    return
                self._stop(row, now, StopCause.IDLE)
            elif (row.state != Lifecycle.STARTING or not isinstance(row.start, DemandStart)
                    or now >= row.start.deadline):
                self._stop(row, now, StopCause.UNKNOWN)
            return
        assert isinstance(observed.boot, VerifiedBoot)
        row.boot = observed.boot
        exposures = [r for r in snapshot.hana_compute if r.generation == row.generation]
        if not exposures:
            self._stop(row, now, StopCause.UNKNOWN)
            return
        exposure = exposures[0]
        renewal_margin = 2 * settings.controller_tick_timeout_s + 3 * settings.tick_interval_s
        expected = row.delivered.generation if isinstance(row.delivered, DeliveredPermit) else 0
        if exposure.expires - now <= renewal_margin and isinstance(row.delivered, DeliveredPermit):
            permission = spending.issue(now, now + spending.settings.permit_max_seconds, row.boot.boot_id,
                expected_generation=row.generation)
        else:
            permission = spending.bind_start(now, row.generation, row.boot.boot_id)
        if isinstance(permission, hana_spend.PermitRefused):
            row.reason = permission.reason
            return
        row.generation = permission.exposure.generation
        if (not isinstance(row.delivered, DeliveredPermit) or row.delivered.generation != row.generation
                or row.delivered.execution != row.claims[0].execution):
            _ = self._intent(row, Action.PUBLISH, now, permit=permission.permit, expected_generation=expected)

    def _accept_permit(self, row: BoxRecord, operation: PendingOperation, reply: PermitAccepted, now: int) -> None:
        if (operation.action != Action.PUBLISH or reply.token != operation.token or reply.identity != row.identity
                or reply.boot != row.boot or reply.generation != operation.generation or reply.execution != operation.execution
                or not row.claims or row.claims[0].execution != reply.execution
                or not isinstance(operation.permit, hana_guest.WorkPermit) or now >= operation.permit.expires_at):
            raise ValueError('The permit acknowledgment has a stale operation, claim, boot identity, or generation')
        row.delivered = DeliveredPermit(reply.generation, int(operation.permit.expires_at), reply.execution)
        claim = row.claims[0]
        row.claims = [WorkClaim(claim.execution, claim.token, claim.requested_at, True)]
        row.state, row.operation = Lifecycle.READY, NoOperation()
