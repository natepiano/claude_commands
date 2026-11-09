import type { On, UiPane } from "claude-code";

export const NOW = Date.UTC(2026, 9, 16, 18, 0);
export const CWD = "/test-worktree";
export const SESSION = "showrunner-session-id";
export const sessionFixture = { cwd: async () => CWD, id: async () => SESSION };
export function hostEngine(on: On, sessionId: () => string | Promise<string> = sessionFixture.id) {
  const panes = new Map<string, UiPane>();
  const opened: string[] = [];
  const closed: string[] = [];
  const forbidden: string[] = [];
  on("session.id", async () => ({ value: await sessionId() }));
  on("session.start", (_$, event) => ({ cwd: event.cwd }));
  on("turn.complete", (_$, event) => ({ text: event.answer }));
  on("command.register", (_$, event) => ({ value: { command: event.name } }));
  on("ui.panes", () => ({ value: [...panes.values()] }));
  on("ui.open", (_$, event) => {
    opened.push(event.id);
    panes.set(event.id, { id: event.id, title: event.title ?? event.id,
      isShown: true, isFocused: true, isPlaced: true });
    return { value: { isPlaced: true } };
  });
  on("ui.close", (_$, event) => {
    closed.push(event.id);
    panes.delete(event.id);
    return { value: undefined };
  });
  on("ui.status", () => { forbidden.push("status"); return { value: undefined }; });
  on("ui.toast", () => { forbidden.push("toast"); return { value: undefined }; });
  return { opened, closed, forbidden };
}

/** Read only displayed text, excluding props, callback ids, and style metadata. */
export function drawnText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(drawnText).join(" ");
  if (typeof value !== "object" || value === null) return "";
  const record = value as Record<string, unknown>;
  const props = typeof record.props === "object" && record.props !== null
    ? record.props as Record<string, unknown> : {};
  return [record.text, record.children, record.label, props.label].map(drawnText).filter(Boolean).join(" ");
}

export function element(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
}

/** Inspect layout rows, inherited styles and host clipping props in one traversal. */
export function inspectDrawing(tree: unknown) {
  type Props = Record<string, unknown>;
  const rows: Props[] = [];
  const textProps: Props[] = [];
  const nodes: Props[] = [];
  const spans: { text: string; color: string; bold: boolean }[] = [];
  const visit = (value: unknown, color = "default", bold = false): string[] => {
    if (typeof value === "string" || typeof value === "number") {
      spans.push({ text: String(value), color, bold });
      return String(value).split("\n");
    }
    if (Array.isArray(value)) return value.flatMap(child => visit(child, color, bold));
    if (typeof value !== "object" || value === null) return [];
    const node = element(value);
    const props = element(node.props);
    nodes.push(node);
    if (node.type === "Text") textProps.push(props);
    if (node.type === "Box" && props.flexDirection === "row") rows.push(props);
    const children = Array.isArray(node.children) ? node.children : node.children === undefined ? [] : [node.children];
    const parts = children.map(child => visit(child,
      typeof props.color === "string" ? props.color : color,
      typeof props.bold === "boolean" ? props.bold : bold)).filter(lines => lines.length);
    if (node.type === "Text") return [parts.flat().join("")];
    if (node.type !== "Box") return parts.flat();
    const top = Number(props.marginTop ?? props.marginY ?? props.margin ?? 0)
      + Number(props.paddingTop ?? props.paddingY ?? props.padding ?? 0);
    const bottom = Number(props.marginBottom ?? props.marginY ?? props.margin ?? 0)
      + Number(props.paddingBottom ?? props.paddingY ?? props.padding ?? 0);
    const lines = props.flexDirection === "column"
      ? parts.flatMap((part, index) => [...Array<string>(index ? Number(props.rowGap ?? props.gap ?? 0) : 0).fill(""), ...part])
      : parts.length || props.flexDirection === "row"
        ? [parts.flat().join(" ".repeat(Number(props.columnGap ?? props.gap ?? 0)))] : [];
    const indent = " ".repeat(Number(props.paddingLeft ?? props.paddingX ?? props.padding ?? 0));
    return [...Array<string>(top).fill(""), ...lines.map(line => indent + line), ...Array<string>(bottom).fill("")];
  };
  const lines = visit(tree);
  return { lines, spans, rows, textProps, nodes };
}

// Keep existing accounts assertions expressed in their original projections.
export const rowLines = (tree: unknown) => inspectDrawing(tree).lines;
export const styledText = (tree: unknown) => inspectDrawing(tree).spans;
export const elements = (tree: unknown) => inspectDrawing(tree).nodes;
