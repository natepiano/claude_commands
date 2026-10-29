"""Auditable question wording, project context, and decision policy."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict


@dataclass(frozen=True)
class Supported:
    threshold: float


@dataclass(frozen=True)
class Unsupported:
    reason: str = "no threshold meets the error target with nonzero coverage"


@dataclass(frozen=True)
class ReportOnly:
    pass


ThresholdOutcome = Supported | Unsupported | ReportOnly

QUESTION_SET_VERSION = "4"
MODEL = "jev-1.14.0"
BUDGET_USD = 20.0
INPUT_USD_PER_MILLION = 0.042
# Complete version-3 tuning run: 20261027T104743.153330Z-3b227095 (N=317).
THRESHOLDS: dict[str, ThresholdOutcome] = {
    "backlog_goal": Supported(0.40),
    "backlog_alignment": Supported(0.40),
    "backlog_impact": Supported(0.40),
    "backlog_urgency": ReportOnly(),
    "backlog_effort": ReportOnly(),
    "category": Supported(0.90),
    # Defined-area options: tune error .083 with .38 taken (2026-10-29).
    "area": Supported(0.90),
}
# The user's per-field verdict after the blind preference test (2026-10-29):
# Jev proposes these fields; the agent proposes every other field.
JEV_FIELDS: frozenset[str] = frozenset({"backlog_goal", "backlog_impact", "backlog_alignment"})

ERROR_TARGETS = {field: 0.1 for field in (
    "backlog_goal", "category", "area", "backlog_impact", "backlog_alignment",
)}
RISK_THRESHOLDS = tuple(percent / 100 for percent in range(40, 100, 5))
EVIDENCE_PRESENT = 0.7
EVIDENCE_ABSENT = 0.3
SPLIT_PROBABILITY = 0.25
BODY_CHARACTER_LIMIT = 24_000

# README summaries; legacy project names remain the vault's identifiers.
PROJECT_ABOUT = {
    "bevy_enhanced_input": "A Bevy observer-based input manager binding keys and other inputs to in-game actions.",
    "biz": "The legal, operational, commercial and financial work of making Hana sustainable.",
    "bevy_brp_mcp": "An MCP server connecting AI tools to Bevy's remote protocol for inspecting and controlling running applications.",
    "bevy_window_manager": "A Bevy plugin for persisting window placement and restoring it across monitors and scale factors.",
    "cargo-berth": "A contributor tool coordinating path ownership and merge order between Git worktrees.",
    "cargo-liner": "A fleet of Cargo contributor tools and their shared terminal UI framework.",
    "cargo-mend": "A contributor tool auditing Rust visibility across crates and workspaces.",
    "cargo-port": "A contributor's terminal dashboard for Rust workspaces, crates, worktrees, tests, CI and machine diagnostics.",
    "hana_conduit": "A Bevy crate in Hana's generative art and visualization performance workspace.",
    "hana_diegetic": "A Bevy layout engine rendering retained UI panels and text directly in 3D space.",
    "fairy_dust": "A helper for Hana's Bevy examples, providing quiet logs and opt-in development conveniences.",
    "hana": "A generative art and visualization performance system for creating, controlling and displaying visuals across devices and machines.",
    "hana_lading": "A Bevy startup disk-asset loader with completion tracking and failure reporting.",
    "bevy_lagrange": "A Bevy camera controller with smooth orbit controls, zoom-to-fit and retained camera journeys.",
    "hana_liminal": "A Bevy plugin rendering 3D mesh outlines with jump-flood and hull-extrusion methods.",
    "nateroids": "A Bevy game, visualization and simulation learning project with physics, intended to be fun, playable and beautiful.",
    "bevy_panorbit_camera": "A Bevy camera controller with smooth orbit, pan and zoom for prototypes, model viewers and multiple viewports.",
    "rust-template": "A cargo-generate template for Rust projects with standardized CI, licensing and a changelog.",
}

GOALS = {
    "ship_hana": "Make Hana a coherent, usable product that can be released and relied on: core capabilities, integration, correctness, usability, documentation and release readiness when they directly advance shipping.",
    "find_collaborators": "Make the work understandable, inviting and practical for collaborators, contributors and partners to join: contribution paths, demonstrations, communication and collaboration infrastructure whose primary outcome is participation.",
    "seek_investors": "Build and communicate a credible investment case and enable investor engagement: product narrative, evidence, fundraising materials and outreach whose primary outcome is investment readiness or engagement.",
    "business_viability": "Make Hana legally, operationally and commercially sustainable: business models, pricing, market analysis, ownership, financial administration, taxes, licensing, payments and intellectual property whose primary outcome is viability.",
}
GOAL_LABELS = dict(zip(GOALS, (
    "1 - Ship Hana", "2 - Find Collaborators", "3 - Seek Investors", "4 - Business Viability",
)))
CATEGORIES = {
    "bug": "Something does not work as it was meant to: it crashes, gives a wrong result or breaks a stated behavior, and the work makes it work as intended. A look or behavior that works as designed but could be better is not a bug.",
    "feature": "The work ships a capability that users or code cannot use at all today, such as a new tool, widget, command or integration, with a defined scope.",
    "enhancement": "The work changes a capability that already exists so it works better, looks better or covers more cases; the issue names the existing thing it improves.",
    "docs": "The deliverable is written documentation, such as a guide, a README or a reference page.",
    "example": "The deliverable is a worked example or a demonstration program that shows how to use something.",
    "refactor": "The work restructures, moves or renames code, or removes an old code path, and users see no change in behavior.",
    "idea": "A creative concept or an inspiration is noted with no scope and no defined deliverable; nobody could start building it from the issue alone.",
    "research": "The deliverable is a finding or a decision, not shipped code: evaluate an outside library, crate, tool or technique, or answer a stated question before an implementation is chosen. An issue that only names an outside project to look at is research.",
    "business": "The work concerns trademarks, funding, recruiting, legal or commercial operations."
}
ALIGNMENT_LEVELS = [
    "The work has a weak or minimal relationship to this goal.",
    "The work indirectly supports this goal or removes a limited obstacle.",
    "The work directly advances a meaningful part of this goal.",
    "Substantial progress on this goal depends on this central work.",
    "Completing `issue` itself delivers a major outcome of this goal.",
]
IMPACT_LEVELS = [
    "Completing the work produces a small or highly localized benefit.",
    "A narrow workflow or audience receives a clear but limited benefit.",
    "An important workflow or audience receives a significant benefit.",
    "A core workflow or several audiences receive a major benefit.",
    "The outcome transforms the product, organization or ecosystem.",
]
EFFORT_LEVELS = [
    "XS: one atomic, tightly scoped action, such as coupling sccache gap start and length in one type or storing CPU usage directly in the CPU pane.",
    "S: contained work with few touchpoints and a known approach, such as a startup phase state lifecycle enum or Cmd-D duplicate for movables.",
    "M: several coordinated steps or touchpoints, such as generic asset loading or the Arrange Jacks widget.",
    "L: broad work with substantial coordination or integration, such as the feature log or panel picking design proposal.",
    "XL: a multi-phase initiative or epic needing decomposition, such as headless widgets v1 or tool graph v1.",
]
# Historical prompt examples stay excluded even after the rubric is revised.
HISTORICAL_EFFORT_EXAMPLE_STEMS = frozenset({
    "board prints pointer to its own json",
    "0.20.0 - handle enum variant-index reflect path (bevy PR 23137)",
    "NonZeroSize",
    "anchor refresh wakes only for its panels",
    "add gamepad camera modes to switcher",
    "audit bevy migration impact",
    "color picker widget",
    "cable auto routing",
    "implement network aware undo",
    "ai graph builder with playback",
})
PRESSURE_LEVELS = [
    "The work can wait; delay has little material cost.",
    "Pressure is building; delay slowly raises cost or loses opportunity.",
    "The work is pressing; delay materially worsens the outcome or problem.",
]
URGENCY_EVIDENCE = {
    "deadline_named": (
        "Does `issue` name a date or event the work must meet?",
        "The work must be completed for a named date or event.",
        "The work is an undated improvement with no event it must meet.",
    ),
    "outside_cutoff": (
        "Does `issue` describe an outside offer or opportunity that ends?",
        "An external offer or opportunity has an explicit ending or cutoff.",
        "The opportunity remains available and no outside cutoff is described.",
    ),
    "current_harm": (
        "Does `issue` describe a failure happening now that stops people from using the product or doing their work?",
        "A failure happens now, and people cannot use the product, finish a task or keep their data until it is fixed; no workaround is given.",
        "The defect is a visible flaw, a wrong look, a slow step or a rare failure with a workaround; people keep using the product and doing their work while it waits.",
    ),
    "blocks_planned_work": (
        "Does `issue` describe other planned work that cannot start until this work is done?",
        "Identified planned work cannot start until `issue` is completed.",
        "Other planned work can start independently of `issue`.",
    ),
}


class NoulQuestion(TypedDict):
    type: Literal["noul"]
    instructions: str
    criteria: dict[str, str]


class ChoiceQuestion(TypedDict):
    type: Literal["choice"]
    instructions: str
    criteria: dict[str, str]


class ScoreQuestion(TypedDict):
    type: Literal["score"]
    instructions: str
    criteria: list[str]


Question = NoulQuestion | ChoiceQuestion | ScoreQuestion


class IssueDescription(TypedDict):
    title: str
    project: str
    project_about: str
    body: str


class IssueState(TypedDict):
    issue: IssueDescription


class AreaState(IssueState):
    areas: list[str]


def _choice_pair(instructions: str, options: dict[str, str]) -> tuple[ChoiceQuestion, ChoiceQuestion]:
    return (
        {"type": "choice", "instructions": instructions, "criteria": dict(options)},
        {"type": "choice", "instructions": instructions, "criteria": dict(reversed(list(options.items())))},
    )


def main_questions() -> dict[str, Question]:
    goal, goal_reverse = _choice_pair("Which goal does `issue` most directly advance? Choose its primary outcome.", GOALS)
    category, category_reverse = _choice_pair("What kind of work does `issue` describe, judged by what the work delivers when it is done?", CATEGORIES)
    result: dict[str, Question] = {
        "goal": goal, "goal_reverse": goal_reverse,
        "category": category, "category_reverse": category_reverse,
        "impact": {"type": "score", "instructions": "How large is the benefit to Hana or its users once the work in `issue` is done? Exclude how soon or how hard the work is.", "criteria": list(IMPACT_LEVELS)},
        "effort": {"type": "score", "instructions": "What relative breadth and coordination does `issue` require? Judge scope, never elapsed time.", "criteria": list(EFFORT_LEVELS)},
        "pressure": {"type": "score", "instructions": "How much cost does delaying `issue` create? Wanting to do it next is not evidence of urgency.", "criteria": list(PRESSURE_LEVELS)},
    }
    for goal_name, definition in GOALS.items():
        result[f"alignment_{goal_name}"] = {"type": "score", "instructions": f"How strongly does `issue` advance this goal: {definition}", "criteria": list(ALIGNMENT_LEVELS)}
    for name, (instructions, yes, no) in URGENCY_EVIDENCE.items():
        result[name] = {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}
    return result


def area_questions(areas: list[str], definitions: Mapping[str, str]) -> dict[str, Question]:
    # An empty vault still has a valid Choice and can explicitly propose unfiled.
    options = {area: (f"The work concerns {area}: {definitions[area]}" if area in definitions
                      else f"The work concerns the part named {area}.")
               for area in areas} or {"unfiled": "The work has no established area."}
    first, reverse = _choice_pair("Which area in `areas` names the part of the work `issue` is about?", options)
    return {
        "area": first, "area_reverse": reverse,
        "area_fits": {"type": "noul", "instructions": "Does one of the areas in `areas` name the part of the work `issue` is about?", "criteria": {
            "true": "An area in `areas` names the part of the work described in `issue`.",
            "false": "The work concerns a part absent from `areas`, or has no specific area.",
        }},
    }
