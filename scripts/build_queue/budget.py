"""Advance the service budget from three state values and current queue evidence."""
from __future__ import annotations

from dataclasses import dataclass, replace

from scripts.lint import memory_admit as gate


@dataclass(frozen=True)
class BudgetState:
    current_budget: int
    last_breach: float
    last_raise: float


def start_budget(config: gate.MemoryConfig, now: float) -> BudgetState:
    return BudgetState(config.build_budget_bytes, now, now)


def advance_budget(state: BudgetState, inputs: gate.QueueInputs, config: gate.MemoryConfig,
                   now: float) -> tuple[BudgetState, gate.AdmissionLimits, gate.BudgetDecision]:
    """Reset on pressure and increase once when only the budget prevents admission."""
    total = inputs.host.total if isinstance(inputs.host, gate.HostMemory) else gate.UnknownHostTotal()
    fixed = gate.resolve_admission_limits(total, config)
    floor = config.build_budget_bytes
    ceiling = config.build_budget_ceiling_bytes
    if isinstance(total, int):
        ceiling = min(ceiling, max(0, total - fixed.reserve))
        floor = min(floor, ceiling)
    else:
        ceiling = floor
    current = min(ceiling, max(floor, state.current_budget))
    reason: gate.BudgetReason = "NoBudgetWaiter"
    change: gate.BudgetReason = "BudgetRaised" if state.last_raise > state.last_breach else "ServiceStarted"
    changed_at = max(state.last_raise, state.last_breach)
    mode: gate.FreeCheckMode = "sum"
    if state.current_budget != current:
        state = BudgetState(current, now, now)
        change, changed_at = "ConfigurationChanged", now
    if isinstance(gate.operator_budget(), int):
        changed_at = state.last_raise if state.current_budget == fixed.budget else now
        state = BudgetState(fixed.budget, now, changed_at)
        reason = change = "OperatorFixedBudget"
    elif (isinstance(inputs.pressure, gate.MemoryPressure)
          or isinstance(inputs.pressure_reading, (gate.MemoryPressure, gate.PressureUnavailable))):
        state = BudgetState(floor, now, state.last_raise)
        reason = "PressureUnavailable" if isinstance(inputs.pressure_reading, gate.PressureUnavailable) else "PressureFloor"
        change, changed_at = reason, now
    elif not isinstance(inputs.host, gate.HostMemory) or not isinstance(total, int):
        state = BudgetState(floor, now, state.last_raise)
        reason = change = "HostUnavailable"
        changed_at = now
    elif now < max(state.last_breach, state.last_raise):
        state = BudgetState(floor, now, now)
        reason = change = "CollectingHistory"
        changed_at = now
    elif now - state.last_breach < config.build_budget_window_seconds:
        state = replace(state, current_budget=current)
        reason = "CollectingHistory"
        if state.last_breach > state.last_raise:
            change = "PressureFloor"
    else:
        mode = config.build_free_check_calm
        state = replace(state, current_budget=current)
        if current >= ceiling:
            reason = "CeilingReached"
        elif now - state.last_raise < config.build_budget_raise_interval_seconds:
            reason = "RaiseInterval"
        elif budget_waiter(inputs, gate.AdmissionLimits(current, fixed.reserve, mode), ceiling, config.policy):
            state = BudgetState(min(ceiling, current + config.build_budget_raise_bytes), state.last_breach, now)
            reason = change = "BudgetRaised"
            changed_at = now
    previous = inputs.budget_decision
    if (reason in ("CollectingHistory", "RaiseInterval", "NoBudgetWaiter", "CeilingReached")
            and isinstance(previous, gate.BudgetDecision) and previous.changed_at == changed_at
            and previous.budget == state.current_budget):
        change = previous.change_reason
    decision = gate.BudgetDecision(state.current_budget, floor, ceiling, changed_at, change, reason, mode)
    return state, gate.AdmissionLimits(state.current_budget, fixed.reserve, mode), decision


def budget_waiter(inputs: gate.QueueInputs, limits: gate.AdmissionLimits, ceiling: int,
                  policy: gate.AdmissionPolicy) -> bool:
    """Remove only the budget limit when checking whether a raise can help."""
    current = gate.admission_pass(inputs, inputs.now, limits, policy)
    enough = max(ceiling, sum(gate.build_charge(step.measured) for step in inputs.running)
                 + sum(row["need"] for _, row in inputs.ordered))
    raised = gate.admission_pass(inputs, inputs.now, replace(limits, budget=enough), policy)
    return any(not row.decision.budget_passes and row.decision.free_passes
               and isinstance(other.admission, gate.ProposedAdmission)
               for row, other in zip(current.rows, raised.rows, strict=True))
