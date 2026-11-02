#!/usr/bin/env python3
"""Publish a change-review page: change_page.py --production DOC [--list L] C27 [C28 ...]."""
from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import change_review as change_review
from scripts.production import showrunners, showrunner_tree
from scripts.production.production_units import Refusal as Refusal, cell_value, production_field, unit_table
from scripts.production.proposals_folder import owner_docs, slug_of
from scripts.feature_log import feature_log
from scripts.review_links import review_link

MANIFEST = ".review-link-changes.json"
PAGE_NAME = "changes.html"


class PageManifest(TypedDict):
    review_list: str
    production: str
    rows: list[str]
    token: str


@dataclass(frozen=True)
class ReviewPage:
    review_list: Path
    production: Path
    rows: tuple[str, ...]
    token: str


class AnswerRefused(ValueError):
    """An invalid answer must not change the review record."""


def page_at(root: Path, request_path: str) -> ReviewPage:
    """Resolve only a published page's private allowlist, never a requested file path."""
    match = re.fullmatch(r"/([a-z0-9]{6})/changes\.html", urlsplit(request_path).path)
    if match is None:
        raise FileNotFoundError("not a change page")
    folder = root / match[1]
    manifest = folder / MANIFEST
    if folder.is_symlink() or manifest.is_symlink():
        raise FileNotFoundError("not a regular page manifest")
    decoded = cast(object, json.loads(manifest.read_text(encoding="utf-8")))
    if not isinstance(decoded, dict):
        raise ValueError("invalid page manifest")
    values = cast(dict[str, object], decoded)
    review_list, production, rows, token = (values.get(key) for key in
                                          ("review_list", "production", "rows", "token"))
    if not (isinstance(review_list, str) and isinstance(production, str)
            and isinstance(token, str) and token and isinstance(rows, list)):
        raise ValueError("invalid page manifest")
    row_values = cast(list[object], rows)
    if not row_values or not all(isinstance(row, str) and re.fullmatch(r"C\d+", row) for row in row_values):
        raise ValueError("invalid page rows")
    return ReviewPage(Path(review_list), Path(production), tuple(cast(list[str], rows)), token)


def shown_rows(page: ReviewPage) -> list[dict[str, str]]:
    rows = {row["#"]: row for row in change_review.review_rows(page.review_list)}
    selected: list[dict[str, str]] = []
    for row_id in page.rows:
        row = rows.get(row_id, {})
        if row.get(change_review.KIND) not in change_review.REVIEW_KINDS:
            raise ValueError(f"{row_id}: classify with change_review.py set-kind before publishing")
        if not row.get("Change") or not row.get(change_review.CHECK):
            raise ValueError(f"{row_id}: needs what changed and how to check / proof")
        selected.append(row)
    return selected


def render(page: ReviewPage) -> bytes:
    sections: list[str] = []
    for row in shown_rows(page):
        row_id = html.escape(row["#"])
        verdict = row.get("Verdict", "")
        answer = (f'''<p role="status">Saved: {html.escape(verdict)}</p>
<form method="post">
<input type="hidden" name="row" value="{row_id}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<input type="hidden" name="feedback" value="">
<button name="outcome" value="retry">Send saved answer again</button>
</form>''' if verdict else f'''
<form method="post">
<input type="hidden" name="row" value="{row_id}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<label for="feedback-{row_id}">Other feedback</label>
<input id="feedback-{row_id}" name="feedback" type="text">
<div><button name="outcome" value="works">Works</button>
<button name="outcome" value="not works">Not works</button>
<button name="outcome" value="feedback">Send feedback</button></div>
</form>''')
        proof_title = "Try it · What you should see" if row[change_review.KIND] == "See in action" else "Proof"
        sections.append(f'''<section id="{row_id}">
<p class="kind">{html.escape(row[change_review.KIND])}</p>
<div class="id">{row_id} · {html.escape(row.get("Merged", ""))}</div>
<h1>{html.escape(row["Change"])}</h1>
<h2>{proof_title}</h2><pre>{html.escape(row[change_review.CHECK])}</pre>
{answer}</section>''')
    return ('''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Changes to review</title><style>
:root{color-scheme:light dark;font:17px/1.55 system-ui,sans-serif}
body{margin:0}main{max-width:680px;margin:auto;padding:40px 16px 64px}
.id,h2{opacity:.7;font-size:14px}.kind{font-weight:600}
h1{font-size:26px;line-height:1.25}h2{margin:28px 0 6px}p{white-space:pre-wrap}
pre{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #8886;border-radius:6px;padding:12px;font:14px/1.45 ui-monospace,monospace}
section+section{border-top:1px solid #8886;margin-top:36px;padding-top:28px}
form{margin-top:28px}label,input{display:block}input[type=text]{box-sizing:border-box;width:100%;font:inherit;margin:6px 0 12px}
button{font:inherit;margin:0 8px 8px 0;padding:6px 12px}
</style></head><body><main>''' + "".join(sections) + "</main></body></html>").encode("utf-8")


def publish(production: Path, review_list: Path, rows: list[str]) -> str:
    page = ReviewPage(review_list.resolve(), production.resolve(), tuple(dict.fromkeys(rows)), secrets.token_urlsafe())
    if not page.rows or any(re.fullmatch(r"C\d+", row) is None for row in page.rows):
        raise ValueError("name one or more row IDs such as C27")
    _ = shown_rows(page)
    _ = showrunner(page)
    manifest: PageManifest = {"review_list": str(page.review_list), "production": str(page.production),
                              "rows": list(page.rows), "token": page.token}
    # A fresh page has its own immutable row allowlist. Only metadata is stored
    # beside the HTML: every GET reads the answers from the review record.
    with tempfile.TemporaryDirectory(prefix="review-changes-") as temporary:
        source = Path(temporary) / PAGE_NAME
        _ = source.write_text("<!doctype html><title>Changes to review</title>", encoding="utf-8")
        url = review_link.snapshot([source], new=True)[0]
    folder = review_link.review_root() / urlsplit(url).path.split("/")[1]
    review_link.replace_file(folder / MANIFEST, json.dumps(manifest).encode("utf-8"))
    return url


def showrunner(page: ReviewPage) -> str:
    """The record names the recipient, including when its production is offline."""
    text = change_review.read(page.review_list)
    frontmatter = text.split("---", 2)
    if len(frontmatter) == 3:
        for line in frontmatter[1].splitlines():
            if line.startswith("showrunner:") and (name := line.partition(":")[2].strip()):
                return name
    raise ValueError("review list has no showrunner in its frontmatter")


def row_production(page: ReviewPage, row: dict[str, str]) -> Path:
    """Resolve the row's owner across the root production's complete hierarchy."""
    root = showrunner_tree.root_of(slug_of(page.production))
    docs = {**showrunners.timer_docs(), slug_of(page.production): page.production}
    if root not in docs:
        raise AnswerRefused(f"root production {root} has no production document")
    owner = row.get(change_review.PRODUCTION) or root
    for doc in owner_docs(docs[root]):
        if slug_of(doc) == owner:
            return doc
    raise AnswerRefused(f"unknown production {owner} for {row['#']}")


def returning_unit(page: ReviewPage, row: dict[str, str]) -> str:
    """Follow the existing verdict workflow: merge -> checkpoint -> Units row."""
    doc = row_production(page, row)
    lines = doc.read_text(encoding="utf-8").splitlines()
    merge = re.search(r"\b[0-9a-f]{7,40}\b", row.get("Merged", ""))
    if merge is None:
        return production_field(lines, "Showrunner session")
    repo = Path(production_field(lines, "Showrunner checkout")).expanduser()
    commits = change_review.merge_commits(repo, merge[0])
    if not commits:
        result = subprocess.run(["git", "-C", str(repo), "show", "-s", "--format=%B", merge[0], "--"],
                                capture_output=True, text=True, check=True)
        message = result.stdout
        commits = [change_review.Commit(merge[0], message.split("\n", 1)[0], message, ())]
    units = unit_table(lines)
    candidates: set[str] = set()
    for commit in commits:
        checkpoint = re.match(r"checkpoint\(([^)]+)\):", commit.subject)
        if checkpoint is None:
            continue
        for unit in units:
            names = (cell_value(unit.get("Branch", "")), cell_value(unit.get("Unit", "")),
                     Path(cell_value(unit.get("Plan", ""))).stem,
                     Path(cell_value(unit.get("Worktree", ""))).name)
            if checkpoint[1] in names:
                name = cell_value(unit["Unit"])
                candidates.add(name)
                if any(isinstance(item, change_review.Visible) and item.change == row["Change"]
                       for item in change_review.entries(commit.message)):
                    return name
    if len(candidates) == 1:
        return next(iter(candidates))
    if candidates:
        raise ValueError("cannot identify one owning unit for this change")
    # A change outside a unit returns to the session that owns that production.
    return production_field(lines, "Showrunner session")


def send_message(recipient: str, sender: str, message: str) -> None:
    """The single outside-send boundary; use the existing routing and delivery rules."""
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "message/send.py"),
                             "--to", recipient, "--from", sender, "--text", message],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "message delivery failed")


def close_feature_log(page: ReviewPage, doc: Path, row_id: str, verdict: str) -> None:
    """Apply step one of CloseChangeReview before the verdict closes the row."""
    projects = {name for name, spec in feature_log.PROJECTS.items()
                if spec.closes_by_change_review}
    lines = doc.read_text(encoding="utf-8").splitlines()
    repo = Path(production_field(lines, "Showrunner checkout")).expanduser()
    script = str(Path(feature_log.__file__))
    project = subprocess.run([sys.executable, script, "project-of", str(repo)],
                             capture_output=True, text=True, check=True).stdout.strip()
    if project not in projects:
        return
    list_name = page.review_list.stem.removeprefix(change_review.PREFIX)
    review = row_id if list_name == "hana" else f"{list_name}/{row_id}"
    command = "add" if change_review.lands(verdict) else "drop"
    arguments = ["--review", review]
    if command == "drop":
        arguments.append(f"--verdict={verdict}")
    result = subprocess.run([sys.executable, script, command, *arguments],
                            capture_output=True, text=True, check=False)
    # The writer names the required operation when a proposal replaces an old note.
    if command == "add" and result.returncode and (
            f"proposal {review} uses supersede; run supersede --review {review}" in result.stderr):
        result = subprocess.run([sys.executable, script, "supersede", *arguments],
                                capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "feature-log operation failed")


def closeout_before_save(page: ReviewPage, doc: Path, row_id: str, verdict: str) -> None:
    """Keep the row open and notify its showrunner when feature-log closeout fails."""
    try:
        close_feature_log(page, doc, row_id, verdict)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        message = f"Change review {row_id} stays open: feature log failed.\n{error}\nList: {page.review_list}"
        try:
            send_message(showrunner(page), Path(__file__).stem, message)
        except RuntimeError as delivery_error:
            raise RuntimeError(f"{message}\nNotification failed: {delivery_error}") from delivery_error
        raise RuntimeError(message) from error


def submit(page: ReviewPage, body: str) -> None:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"row", "token", "outcome", "feedback"} or any(len(values) != 1 for values in fields.values()):
        raise AnswerRefused("expected only row, token, outcome and feedback")
    row_id, token, outcome, feedback = (fields[key][0] for key in ("row", "token", "outcome", "feedback"))
    if row_id not in page.rows or not secrets.compare_digest(token.encode("utf-8"), page.token.encode("utf-8")):
        raise AnswerRefused("row or page token does not match this page")
    if outcome not in ("works", "not works", "feedback", "retry") or "\n" in feedback or "\r" in feedback:
        raise AnswerRefused("choose an answer and use one line of feedback")
    if outcome == "feedback" and not feedback.strip():
        raise AnswerRefused("enter feedback")
    verdict = feedback.strip() if feedback.strip() else outcome
    if outcome in ("works", "not works") and feedback.strip():
        verdict = f"{outcome}: {feedback.strip()}"
    row = next(row for row in shown_rows(page) if row["#"] == row_id)
    if outcome == "retry":
        if not row.get("Verdict") or feedback:
            raise AnswerRefused("retry requires a saved answer and no new feedback")
        verdict = row["Verdict"]
    if row.get("Verdict") and row["Verdict"] != verdict:
        raise AnswerRefused("this row already has a different saved answer")
    recipient = showrunner(page)
    doc = row_production(page, row)
    unit = returning_unit(page, row) if verdict != "works" else recipient
    if not row.get("Verdict"):
        closeout_before_save(page, doc, row_id, verdict)
    result = subprocess.run([sys.executable, str(Path(change_review.__file__)), "verdict",
                             "--production", str(page.production), "--list", str(page.review_list),
                             "--row", row_id, f"--verdict={verdict}"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise AnswerRefused((result.stderr or result.stdout).strip())
    message = (f"Change review {row_id}: {verdict}\n"
               f"List: {page.review_list}\nProduction: {page.production}\n"
               f"Change: {row['Change']}\nMerged: {row.get('Merged', '')}\n"
               f"User feedback: {feedback if feedback.strip() else verdict}\n{result.stdout.strip()}")
    errors: list[str] = []
    for target in dict.fromkeys((unit, recipient)):
        try:
            send_message(target, Path(__file__).stem, message)
        except RuntimeError as error:
            errors.append(f"{target}: {error}")
    if errors:
        raise RuntimeError("Answer saved; delivery failed. Reload the page and select Send saved answer again. "
                           + "; ".join(errors))


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("rows", nargs="+")
    _ = parser.add_argument("--production", type=Path, required=True)
    _ = parser.add_argument("--list", type=Path)
    args = parser.parse_args(arguments)
    production = cast(Path, args.production).expanduser()
    review_list = cast(Path | None, args.list)
    try:
        path = review_list.expanduser() if review_list else change_review.production_list(str(production))
        print(publish(production, path, cast(list[str], args.rows)))
    except (OSError, ValueError, change_review.ChangeReviewError, subprocess.SubprocessError, Refusal) as error:
        print(f"change page: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
