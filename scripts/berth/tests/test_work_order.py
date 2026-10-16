#!/usr/bin/env python3
"""Work Order validation: the optional Workers field and lettered phases."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from berth.work_order import (  # noqa: E402
    EveryWorkOrderSelection,
    ExactPhaseSelection,
    ValidatedWorkOrder,
    WorkOrderSelection,
    WorkOrderValidationError,
    _validated_orders,  # pyright: ignore[reportPrivateUsage]
)


def _plan(workers_block: str) -> str:
    # Dedent the template before substituting: an interpolated block at column
    # zero would otherwise defeat the common-prefix strip and leave every
    # heading indented.
    template = textwrap.dedent(
        """\
        # Feature

        > **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** one line

        ## Delegation Context

        - **Project:** demo

        ## Phases

        ### Phase 1 — Replay  · status: todo

        #### Work Order

        **Goal:** replay works.

        **Spec:**
        Replay the log.

        **Files:**
        - `src/replay.rs` — the replay loop
        @@WORKERS@@
        **Acceptance gate:** `bash ~/.claude/scripts/delegate/verify.sh test demo`
        """
    )
    return template.replace("@@WORKERS@@", workers_block)


class WorkersFieldTest(unittest.TestCase):
    def _validate(self, workers_block: str) -> None:
        with tempfile.TemporaryDirectory() as root:
            plan = Path(root) / "plan.md"
            _ = plan.write_text(_plan(workers_block), encoding="utf-8")
            _ = _validated_orders(str(plan), Path(root), ExactPhaseSelection("1"))

    def test_absent_workers_validates(self) -> None:
        self._validate("")

    def test_present_workers_validates(self) -> None:
        self._validate(
            textwrap.dedent(
                """
                **Workers:** 1 writer + 1 tester — everything lands in `src/replay.rs`
                - `impl` — `src/replay.rs`
                - `test` — `tests/replay.rs`, from Spec
                """
            )
        )

    def test_empty_workers_fails(self) -> None:
        with self.assertRaises(WorkOrderValidationError) as caught:
            self._validate("\n**Workers:**\n")
        self.assertIn("empty **Workers:** section", " ".join(caught.exception.errors))

    def test_in_flight_plan_with_the_old_field_still_validates(self) -> None:
        self._validate("\n**Seats:** 2 writers\n- `impl` — `src/replay.rs`\n")

    def test_old_field_is_not_validated_as_a_worker_assignment(self) -> None:
        self._validate("\n**Seats:**\n")


def _phase(identifier: str) -> str:
    return textwrap.dedent(
        f"""\
        ### Phase {identifier} — Replay  · status: todo

        #### Work Order

        **Goal:** phase {identifier} works.

        **Spec:**
        Replay the log.

        **Files:**
        - `src/phase_{identifier}.rs` — the replay loop

        """
    )


class LetteredPhaseTest(unittest.TestCase):
    def _orders(
        self, identifiers: tuple[str, ...], selection: WorkOrderSelection
    ) -> tuple[ValidatedWorkOrder, ...]:
        with tempfile.TemporaryDirectory() as root:
            plan = Path(root) / "plan.md"
            _ = plan.write_text(
                "# Feature\n\n## Phases\n\n" + "".join(_phase(i) for i in identifiers),
                encoding="utf-8",
            )
            _, orders = _validated_orders(str(plan), Path(root), selection)
            return orders

    def test_lettered_phase_validates(self) -> None:
        orders = self._orders(("46c",), EveryWorkOrderSelection())
        self.assertEqual([order.phase for order in orders], ["46c"])

    def test_selection_picks_only_its_own_phase(self) -> None:
        for selector in ("46", "46c"):
            with self.subTest(selector=selector):
                orders = self._orders(("46", "46c"), ExactPhaseSelection(selector))
                self.assertEqual([order.phase for order in orders], [selector])
                self.assertEqual(orders[0].goal, f"phase {selector} works.")


if __name__ == "__main__":
    _ = unittest.main()
