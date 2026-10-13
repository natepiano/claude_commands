"""File texts for the isolated, three-unit textkit production."""

from dataclasses import dataclass
from pathlib import Path

TRIAL_SCRIPT = "/home/natepiano/worktrees/claude-build-followups-herdr-trial/scripts/herdr/trial.py"
TRIAL_RULE = (
    f"Trial production: units run in herdr session herdr-trial, launched by `{TRIAL_SCRIPT}` "
    "(the worktree copy carries every later phase's changes before promotion); never launch, "
    "restart or look up a unit through tmux or `add_unit.py`; log anything that assumes tmux "
    "with `trial.py issue`; never promote or wrap; never answer a trust or permission prompt for the user."
)


@dataclass(frozen=True)
class UnitSpecification:
    slug: str
    signature: str
    behavior: str
    tests: str

    @property
    def name(self) -> str:
        return f"{self.slug}-unit"


UNITS = (
    UnitSpecification("slug", "slugify(text) -> str",
                      "Lowercase text, replace each run of non-ASCII-alphanumeric characters with " +
                      "one hyphen, and strip leading and trailing hyphens.",
                      '`slugify(" Hello, World! ") == "hello-world"` and `slugify("") == ""`'),
    UnitSpecification("clamp", "clamp(value, low, high) -> float",
                      "Return value constrained to the inclusive bounds low and high as a float. " +
                      "Inputs have low <= high.",
                      "`clamp(-2, 0, 10) == 0.0` and `clamp(12, 0, 10) == 10.0`"),
    UnitSpecification("count", "word_count(text) -> int",
                      "Count words separated by whitespace using text.split().",
                      '`word_count("one  two\\nthree") == 3` and `word_count("") == 0`'),
)


def production_plan(root: Path) -> str:
    repo = root / "repo"
    rows = "\n".join(
        f"| {unit.name} | `docs/plans/herdr-fake-{unit.slug}.md` | " +
        f"`{root / 'worktrees' / unit.name}` | `herdr-fake-{unit.slug}` | — | " +
        "`textkit/__init__.py`, `textkit/test_textkit.py` |"
        for unit in UNITS
    )
    return f"""# Production — herdr-fake

> **Status: PRODUCTION — running.**

## Production Context

- **Repository:** `{repo}`
- **Merge branch:** `herdr-fake`
- **Showrunner checkout:** `{repo}`
- **Showrunner session:** natedev
- **Log:** `docs/plans/herdr-fake-log.md`
- **User zone:** America/New_York
- **Updates:** on demand
- **Merge tests:** `python3 -m unittest discover -s textkit`
- **Push:** `git` (bare origin `{root / 'origin.git'}`)
- **Promote:** none

## Units

| Unit | Plan | Worktree | Branch | Port | Owns |
| --- | --- | --- | --- | --- | --- |
{rows}

## Production rules

{TRIAL_RULE}
"""


def unit_plan(root: Path, unit: UnitSpecification) -> str:
    worktree = root / "worktrees" / unit.name
    return f"""# Herdr fake {unit.slug}

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Add one textkit helper.
> **Production: herdr-fake** — unit `{unit.name}`; production doc `docs/plans/herdr-fake-production.md`

## Delegation Context

- **Project:** textkit — a tiny trial production under `{root}`.
- **Stack:** Python 3 standard library.
- **Worktree:** `{worktree}`; branch `herdr-fake-{unit.slug}`. Name both in every dispatch.
- **Layout:** `textkit/__init__.py` holds helpers; `textkit/test_textkit.py` holds unittest cases.
- **Key files:** those two files; preserve the existing identity helper and tests.
- **Test:** `python3 -m unittest discover -s textkit`
- **Workers:** two existing panes, `{unit.name}-impl` and `{unit.name}-review`, in herdr session `herdr-trial`. Reach them with `python3 {TRIAL_SCRIPT} work {unit.name} impl|review <work-order-file>` and await them with `python3 {TRIAL_SCRIPT} wait {unit.name} impl|review`, in place of `implement.sh` and `review.sh` seats. Write each work order to an absolute file before sending it.
- **Invariants:** only scratch work; never use tmux or add_unit.py to launch, restart or locate units; never promote or wrap; never answer a trust or permission prompt. Log such assumptions with `trial.py issue`.

## Phases

### Phase 1 — Add {unit.signature.split('(')[0]} · status: todo

#### Work Order

**Goal:** `{unit.signature}` exists and its two tests pass.

**Spec:** Add `{unit.signature}` in `textkit/__init__.py`. {unit.behavior} Add exactly two unittest cases in `textkit/test_textkit.py`: {unit.tests}. Keep other helpers and tests intact. Work in `{worktree}`, branch `herdr-fake-{unit.slug}`.

**Files:**
- `textkit/__init__.py` — helper implementation.
- `textkit/test_textkit.py` — two tests.

**Seats:** 1 writer + 1 reviewer, the already-running `{unit.name}-impl` and `{unit.name}-review` panes. Dispatch and await through `trial.py work` and `trial.py wait`, never delegate seat scripts. The impl worker edits both named files; review reads the resulting diff and reports findings.

**Acceptance gate:** `python3 -m unittest discover -s textkit` passes, including both new tests.
"""


def file_texts(root: Path) -> dict[str, str]:
    files = {
        "README.md": "# textkit\n\nTiny scratch production for the isolated herdr trial.\n" +
                     "\nRun `python3 -m unittest discover -s textkit`.\n",
        "textkit/__init__.py": '"""Small text helpers for the herdr trial."""\n\n' +
                               "def identity(text: str) -> str:\n    return text\n",
        "textkit/test_textkit.py": "import unittest\n\nfrom textkit import identity\n\n\n" +
                                  "class TextkitTests(unittest.TestCase):\n" +
                                  "    def test_identity(self) -> None:\n" +
                                  "        self.assertEqual(identity('hello'), 'hello')\n",
        "docs/plans/herdr-fake-production.md": production_plan(root),
        "docs/plans/herdr-fake-log.md": "# Herdr fake production log\n",
    }
    for unit in UNITS:
        files[f"docs/plans/herdr-fake-{unit.slug}.md"] = unit_plan(root, unit)
    return files
