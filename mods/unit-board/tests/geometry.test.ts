import { describe, expect, test } from "claude-code/testing";
import { cellWidth, doneText, fitText, labelRuns, overdueText, rulerCells, runsOf, stripCells } from "../hooks/geometry";
import { fixture, NOW, phase, time } from "./kit";

describe("board geometry", () => {
  for (const [minutes, expected] of [[59, "59m"], [60, "1h 0m"], [320, "5h 20m"], [1500, "1d 1h"]] as const) {
    test(`a promise overdue by ${minutes} minutes uses a compact duration`, () => {
      expect(overdueText(NOW / 1000 - minutes * 60, NOW / 1000)).toBe(` · overdue ${expected}`);
    });
  }

  test("terminal widths count wide characters and emoji twice and combining marks zero times", () => {
    expect(cellWidth("A界Ａ😀e\u0301")).toBe(8);
    expect(cellWidth("\u0301\u200d\ufe0f")).toBe(0);
    expect(fitText("界".repeat(30), 20)).toBe("界".repeat(9) + "…");
    expect(cellWidth(fitText("界".repeat(30), 20))).toBeLessThanOrEqual(20);
    expect(fitText("😀😀", 3)).toBe("😀…");
    expect(fitText("e\u0301xy", 2)).toBe("e\u0301…");
    expect(fitText("界", 2)).toBe("界");
    expect(fitText("界", 1)).toBe("…");
  });

  test("fold and more-running lines reserve terminal cells for whole keys and overflow counts", () => {
    expect(doneText(["界界", "ab"], 14)).toBe("Done: 界界, ab");
    expect(doneText(["界界", "ab"], 13)).toBe("Done: +2 more");
    expect(doneText(["界界", "ab"], 25, "+2 more running")).toBe("+2 more running: 界界, ab");
    const overflow = doneText(["界界界界", "ab"], 24, "+2 more running");
    expect(overflow).toBe("+2 more running: +2 more");
    expect(cellWidth(overflow)).toBeLessThanOrEqual(24);
  });

  test("title fitting reserves a complete following status column at the width boundary", () => {
    const title = "A long phase title that would collide with its status";
    const status = "  open";
    const row = fitText(title, 30 - status.length) + status;
    expect(row.length).toBe(30);
    expect(row).toBe("A long phase title that…  open");
    expect(fitText("Short phase", 11)).toBe("Short phase");
    expect(fitText(title, 1)).toBe("…");
    expect(fitText(title, 0)).toBe("");
  });

  test("Done folding preserves whole names at exact fit and counts every omitted unit", () => {
    expect(doneText(["alpha", "beta"], 17)).toBe("Done: alpha, beta");
    expect(doneText(["alpha", "beta"], 16)).toBe("Done: +2 more");
    // Later short keys must not jump ahead of the first key that cannot fit.
    expect(doneText(["long-unit-name", "b", "c"], 13)).toBe("Done: +3 more");
  });

  test("standing-by folding reserves its own label and counts overflow without skipping names", () => {
    expect(doneText(["alpha", "beta"], 24, "Standing by")).toBe("Standing by: alpha, beta");
    expect(doneText(["alpha", "beta"], 23, "Standing by")).toBe("Standing by: +2 more");
    expect(doneText(["long-unit-name", "b", "c"], 20, "Standing by")).toBe("Standing by: +3 more");
  });

  test("phase strip distinguishes each state and keeps large plans readable", () => {
    const small = stripCells([phase("1", "done"), phase("2", "open"),
      phase("3", "todo"), phase("4", "gated")]);
    expect(small.map(cell => cell.ch).join("")).toBe("█▓░▒");
    const large = Array.from({ length: 45 }, (_, index) => phase(String(index + 1), index < 44 ? "done" : "todo"));
    expect(stripCells(large).map(cell => cell.ch).join("")).toContain("44/45 done");
  });

  test("ruler starts at phase start and leaves ten percent beyond the latest horizon", () => {
    const unit = fixture().units[0]!;
    const before = rulerCells(unit, NOW / 1000, 100, -240);
    expect(before.from).toBe(NOW / 1000 - 3600);
    expect(before.to).toBe(NOW / 1000 + 1800 + 540);
    const late = rulerCells(unit, NOW / 1000 + 3600, 100, -240);
    expect(late.to).toBe(NOW / 1000 + 3600 + 720);
    expect(late.track.length).toBe(100);
  });

  test("ruler starts at the open retry when an earlier attempt has the same phase id", () => {
    const unit = fixture().units[0]!;
    const completed = phase("2", "done");
    completed.start = time(-120, "16:00");
    completed.finish = time(-90, "16:30");
    const retry = phase("2", "open");
    retry.start = time(-10, "17:50");
    unit.phases = [completed, retry];
    unit.eta!.trail = [];

    const ruler = rulerCells(unit, NOW / 1000, 100, -240);

    expect(ruler.from).toBe(retry.start.at);
    expect(ruler.to).toBe(NOW / 1000 + 1800 + 240);
    expect(ruler.labels).toContain("17:50");
    expect(ruler.labels).not.toContain("16:00");
  });

  test("late ruler draws the original promise and moves now independently of schedule end", () => {
    const unit = fixture().units[0]!;
    unit.eta!.schedule_end = time(120, "20:00");
    const ruler = rulerCells(unit, NOW / 1000 + 3600, 100, -240);
    const promise = ruler.track.findIndex(cell => cell.ch === "●");
    const now = ruler.track.findIndex(cell => cell.ch === "│");
    expect(promise).toBeGreaterThan(0);
    expect(now).toBeGreaterThan(promise);
    expect(ruler.track[now]?.tone).toBe("error");
    expect(ruler.track.filter(cell => cell.ch === "○").length).toBe(2);
    expect(ruler.track.some(cell => cell.ch === "━")).toBe(true);
    expect(ruler.labels).toContain("15:00");
    expect(ruler.to).toBe(NOW / 1000 + 3600 + 720);
  });

  test("overlapping labels are dropped whole and the later mark wins", () => {
    const labels = labelRuns([{ column: 2, text: "FIRST" }, { column: 4, text: "LAST" },
      { column: 20, text: "END" }], 30);
    expect(labels).not.toContain("FIRST");
    expect(labels).toContain("LAST");
    expect(labels).toContain("END");
    expect(labels.length).toBeLessThanOrEqual(30);
  });

  test("style runs preserve every glyph and split exactly where its theme tone changes", () => {
    const runs = runsOf([{ ch: "━", tone: "success" }, { ch: "━", tone: "success" },
      { ch: "│", tone: "error" }, { ch: "─", tone: "subtle" }]);
    expect(runs).toEqual([{ text: "━━", tone: "success" }, { text: "│", tone: "error" },
      { text: "─", tone: "subtle" }]);
  });
});
