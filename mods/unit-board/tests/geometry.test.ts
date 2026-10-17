import { describe, expect, test } from "claude-code/testing";
import { labelRuns, rulerCells, runsOf, stripCells } from "../hooks/geometry";
import { fixture, NOW, phase, time } from "./kit";

describe("board geometry", () => {
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
