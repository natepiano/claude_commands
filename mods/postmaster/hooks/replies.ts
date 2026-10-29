import type { SessionReceiveInput } from "claude-code";
import { stateDirectory } from "./outbox";
import { PATHS, REPLY_WINDOW_MS, isDeliveryRouteFile } from "./protocol";
import type { DeliveryRoute, DeliveryRouteFile, PostmasterCalls, Replies, ReplyDestination, ReplyLog } from "./protocol";

type ReplyWrapper = { kind: "reply"; socket: string; name: string; body: string } | { kind: "not a reply" };

function wrapper(text: string): ReplyWrapper {
  const lines = text.split("\n");
  const first = lines[0];
  const last = lines[lines.length - 1];
  if (first === undefined || !/^<cross-session-message\s.*>\r?$/.test(first)
    || last?.replace(/\r$/, "") !== "</cross-session-message>") return { kind: "not a reply" };
  const socket = /\sfrom="([^"]+)"/.exec(first)?.[1];
  const name = /\sfrom-name="([^"]+)"/.exec(first)?.[1];
  if (socket === undefined || !socket.startsWith("uds:/") || name === undefined) return { kind: "not a reply" };
  return { kind: "reply", socket, name, body: lines.slice(1, -1).join("\n") };
}

function destination(sessionId: string): ReplyDestination {
  return sessionId === "" ? { kind: "unroutable" } : { kind: "session", sessionId };
}

function routeFile(route: DeliveryRoute, status: DeliveryRouteFile["route"]): DeliveryRouteFile {
  return {
    route: status, socket: route.socket, sender: route.sender,
    reply_to_session_id: route.destination.kind === "session" ? route.destination.sessionId : "",
    session_id: route.sessionId, at_ms: route.atMs,
  };
}

export async function startReplies(calls: PostmasterCalls): Promise<Replies> {
  const directory = await stateDirectory(calls);
  const routes = new Map<string, Map<string, DeliveryRoute>>();
  const pendingWithdrawals = new Map<string, DeliveryRoute>();
  let routeCount = 0;
  let replyCount = 0;
  let forwardCount = 0;
  const path = (folder: string, key: string) => `${directory}/${folder}/${key}.json`;
  const inWindow = (atMs: number, now: number) => atMs <= now && atMs >= now - REPLY_WINDOW_MS;
  const remember = (route: DeliveryRoute): void => {
    let socketRoutes = routes.get(route.socket);
    if (socketRoutes === undefined) {
      socketRoutes = new Map();
      routes.set(route.socket, socketRoutes);
    }
    socketRoutes.set(route.key, route);
  };
  const expire = (now: number): void => {
    for (const [socket, socketRoutes] of routes) {
      for (const [key, route] of socketRoutes) if (!inWindow(route.atMs, now)) socketRoutes.delete(key);
      if (socketRoutes.size === 0) routes.delete(socket);
    }
  };
  const delivered = `${directory}/${PATHS.delivered}`;
  const present = await calls.fs.exists(delivered);
  const files = await calls.fs.list(delivered).catch(error => {
    if (present) throw error;
    return [];
  });
  const now = calls.now();
  for (const file of files) {
    if (file.kind !== "file" || file.isLink || !/^\d+-\d+\.json$/.test(file.name)) continue;
    const key = file.name.slice(0, -5);
    const count = Number(key.slice(key.lastIndexOf("-") + 1));
    if (Number.isSafeInteger(count)) routeCount = Math.max(routeCount, count);
    try {
      const value: unknown = JSON.parse(await calls.fs.read(path(PATHS.delivered, key)));
      if (!isDeliveryRouteFile(value) || value.route !== "open" || !inWindow(value.at_ms, now)) continue;
      remember({ key, socket: value.socket, sender: value.sender, destination: destination(value.reply_to_session_id), sessionId: value.session_id, atMs: value.at_ms });
    } catch { /* An unreadable route cannot identify a reply. */ }
  }

  return {
    sending: async (entry, atMs) => {
      expire(atMs);
      const route: DeliveryRoute = {
        key: `${atMs}-${++routeCount}`, socket: entry.socket, sender: entry.sender,
        destination: destination(entry.reply_to_session_id), sessionId: entry.session_id, atMs,
      };
      await calls.fs.write(path(PATHS.delivered, route.key), JSON.stringify(routeFile(route, "open")));
      remember(route);
      return { key: route.key };
    },
    notDelivered: async token => {
      let route = pendingWithdrawals.get(token.key);
      if (route === undefined) {
        for (const [socket, socketRoutes] of routes) {
          route = socketRoutes.get(token.key);
          if (route === undefined) continue;
          socketRoutes.delete(token.key);
          if (socketRoutes.size === 0) routes.delete(socket);
          pendingWithdrawals.set(token.key, route);
          break;
        }
      }
      if (route === undefined) return;
      await calls.fs.write(path(PATHS.delivered, token.key), JSON.stringify(routeFile(route, "withdrawn")));
      pendingWithdrawals.delete(token.key);
    },
    receive: async (e: SessionReceiveInput) => {
      if (e.origin.kind !== "peer") return { kind: "pass" };
      const reply = wrapper(e.text);
      if (reply.kind === "not a reply") return { kind: "pass" };
      const at_ms = calls.now();
      expire(at_ms);
      const socketRoutes = routes.get(reply.socket);
      if (socketRoutes === undefined) return { kind: "pass" };
      const matching = [...socketRoutes.values()];
      const senders = [...new Set(matching.map(route => route.sender))];
      const targets = new Set<string>();
      for (const route of matching) if (route.destination.kind === "session") targets.add(route.destination.sessionId);
      const replying = matching[matching.length - 1];
      if (replying === undefined) return { kind: "pass" };
      const answerTo = senders.length === 1 ? senders.join(", ") : `one of ${senders.join(", ")}`;
      const logPath = path(PATHS.replies, `${at_ms}-${++replyCount}`);
      const log: ReplyLog = {
        at_ms, socket: reply.socket, name: reply.name, session_id: replying.sessionId, senders, body: reply.body,
        targets: [...targets], forward: "pending", forwarded: [],
      };
      // The record exists before any forward, so a recipient's hook can check where the reply came from.
      await calls.fs.write(logPath, JSON.stringify(log));
      for (const sessionId of targets) {
        const text = `Message from ${reply.name} (their answer to ${answerTo}, passed on by postmaster) [${at_ms}-${++forwardCount}]:\n\n${reply.body}`;
        try {
          const answer = await calls.session.send({ to: { sessionId }, text });
          if (answer.isDelivered) log.forwarded.push(sessionId);
        } catch { /* The answer is still logged and consumed when forwarding fails. */ }
      }
      log.forward = "done";
      await calls.fs.write(logPath, JSON.stringify(log));
      return { kind: "consumed", reason: "postmaster reply" };
    },
  };
}
