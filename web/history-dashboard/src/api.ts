import { pageConfig } from "./config";
import type { HistoryEnvelope, Pagination, Project, Session, Turn } from "./types";

interface ProjectsPayload {
  projects: Project[];
  pagination: Pagination;
}

interface SessionsPayload {
  project: string;
  sessions: Session[];
  pagination: Pagination;
}

interface TurnsPayload {
  project: string;
  chat_id: string;
  turns: Turn[];
  pagination: Pagination;
}

interface TurnPayload {
  turn: Turn;
}

function buildUrl(path: string, params: Record<string, string | number | boolean | undefined>) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    url.searchParams.set(key, String(value));
  });
  if (pageConfig.authToken) {
    url.searchParams.set("token", pageConfig.authToken);
  }
  return url.toString();
}

async function fetchJson<T>(path: string, params: Record<string, string | number | boolean | undefined>) {
  const response = await fetch(buildUrl(path, params), { credentials: "include" });
  if (response.status === 401) {
    window.location.href = "/history/entry?next=/history";
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `request failed: ${response.status}`);
  }
  return (await response.json()) as HistoryEnvelope<T>;
}

export const api = {
  getProjects(offset = 0, limit = 50) {
    return fetchJson<ProjectsPayload>("/history/api/projects", { offset, limit });
  },
  getSessions(project: string, offset = 0, limit = 50) {
    return fetchJson<SessionsPayload>("/history/api/sessions", { project, offset, limit });
  },
  getTurns(project: string, chatId: string, offset: number, limit: number) {
    return fetchJson<TurnsPayload>("/history/api/turns", {
      project,
      chat_id: chatId,
      offset,
      limit,
      include_events: false
    });
  },
  getTurn(turnId: string) {
    return fetchJson<TurnPayload>("/history/api/turn", {
      turn_id: turnId,
      include_events: true
    });
  }
};
