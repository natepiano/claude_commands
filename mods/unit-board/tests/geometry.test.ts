import { describe, expect, test } from "claude-code/testing";
import { cellWidth } from "../hooks/geometry";

describe("shared pane geometry", () => {
  test("terminal widths count wide characters and emoji twice and combining marks zero times", () => {
    expect(cellWidth("A界Ａ😀e\u0301")).toBe(8);
    expect(cellWidth("\u0301\u200d\ufe0f")).toBe(0);
  });
});
