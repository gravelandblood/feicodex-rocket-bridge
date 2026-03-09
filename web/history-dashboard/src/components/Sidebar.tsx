import React, { useEffect, useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Clock, Folder, LogOut, XCircle } from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";

import { api } from "../api";
import { pageConfig } from "../config";
import type { Pagination, Project, Session } from "../types";

type SessionBag = {
  items: Session[];
  pagination: Pagination | null;
};

export function Sidebar({
  selectedSession,
  onSelectSession
}: {
  selectedSession: Session | null;
  onSelectSession: (session: Session) => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [sessionsByProject, setSessionsByProject] = useState<Record<string, SessionBag>>({});
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [loadingSessions, setLoadingSessions] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const response = await api.getProjects();
        const projectItems = response.data.projects;
        setProjects(projectItems);
        const preferredProject =
          (pageConfig.initialProject && projectItems.find((item) => item.name === pageConfig.initialProject)?.name) ||
          projectItems[projectItems.length - 1]?.name ||
          "";
        if (preferredProject) {
          await toggleProject(preferredProject, true);
        }
      } catch (err) {
        setError(String((err as Error).message || err));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const toggleProject = async (projectName: string, autoSelectSession = false) => {
    const nextExpanded = new Set(expandedProjects);
    if (nextExpanded.has(projectName)) {
      nextExpanded.delete(projectName);
      setExpandedProjects(nextExpanded);
      return;
    }

    nextExpanded.add(projectName);
    setExpandedProjects(nextExpanded);

    if (!sessionsByProject[projectName]) {
      setLoadingSessions((prev) => new Set(prev).add(projectName));
      try {
        const response = await api.getSessions(projectName);
        const nextBag = { items: response.data.sessions, pagination: response.data.pagination };
        setSessionsByProject((prev) => ({ ...prev, [projectName]: nextBag }));

        if (autoSelectSession && nextBag.items.length > 0) {
          const preferredSession =
            (pageConfig.initialChatId &&
              nextBag.items.find((item) => item.chat_id === pageConfig.initialChatId && item.project === projectName)) ||
            nextBag.items[nextBag.items.length - 1];
          onSelectSession(preferredSession);
        }
      } finally {
        setLoadingSessions((prev) => {
          const next = new Set(prev);
          next.delete(projectName);
          return next;
        });
      }
    } else if (autoSelectSession && sessionsByProject[projectName].items.length > 0 && !selectedSession) {
      onSelectSession(sessionsByProject[projectName].items[sessionsByProject[projectName].items.length - 1]);
    }
  };

  if (loading) {
    return <div className="p-4 text-sm text-gray-500">加载项目中...</div>;
  }

  return (
    <div className="flex h-full flex-col bg-gray-50/30">
      <div className="sticky top-0 z-10 border-b border-gray-200 bg-white p-4">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-lg font-semibold tracking-tight text-gray-900">项目看板</h1>
          <a
            href="/history/logout"
            className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-gray-50 px-3 py-1.5 text-xs text-gray-600 transition-colors hover:bg-gray-100"
          >
            <LogOut size={14} />
            退出
          </a>
        </div>
        {error ? <div className="mt-3 rounded-lg bg-red-50 p-3 text-xs text-red-600">{error}</div> : null}
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto p-3">
        {projects.map((project) => {
          const isExpanded = expandedProjects.has(project.name);
          const sessions = sessionsByProject[project.name]?.items || [];
          const pagination = sessionsByProject[project.name]?.pagination;
          const isLoadingSessions = loadingSessions.has(project.name);

          return (
            <div key={project.name} className="flex flex-col">
              <button
                onClick={() => void toggleProject(project.name)}
                className={`flex w-full items-center gap-3 rounded-lg p-2.5 text-left transition-all hover:bg-gray-100 ${
                  isExpanded ? "bg-gray-100/50" : ""
                }`}
              >
                <div className="text-gray-400">{isExpanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</div>
                <div className="flex-shrink-0 rounded-md bg-indigo-50 p-1.5 text-indigo-600">
                  <Folder size={16} strokeWidth={2.5} />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-gray-900">{project.name}</div>
                  <div className="mt-0.5 text-[10px] text-gray-400">
                    {project.session_count} 会话 · {formatDistanceToNow(project.updated_at * 1000, { addSuffix: true, locale: zhCN })}
                  </div>
                </div>
                <div className="flex items-center gap-1 rounded border border-gray-200 bg-white px-1.5 py-0.5 text-[10px] text-gray-400 shadow-sm">
                  {project.session_count}
                </div>
              </button>

              {isExpanded ? (
                <div className="mt-1 mb-2 ml-6 space-y-1 border-l-2 border-gray-100 pl-3">
                  {isLoadingSessions ? <div className="p-2 text-xs text-gray-400">加载会话中...</div> : null}
                  {!isLoadingSessions && sessions.length === 0 ? <div className="p-2 text-xs text-gray-400">暂无会话</div> : null}
                  {sessions.map((session) => {
                    const isActive =
                      selectedSession?.chat_id === session.chat_id && selectedSession?.project === session.project;
                    return (
                      <button
                        key={`${session.project}:${session.chat_id}`}
                        onClick={() => onSelectSession(session)}
                        className={`flex w-full flex-col gap-1.5 rounded-lg p-2.5 text-left transition-all ${
                          isActive
                            ? "bg-indigo-50 text-indigo-900 shadow-sm ring-1 ring-indigo-500/20"
                            : "text-gray-700 hover:bg-gray-100"
                        }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="line-clamp-2 text-xs font-medium leading-snug">{session.display_title}</div>
                          {session.latest_status === "completed" ? (
                            <CheckCircle2 size={14} className="mt-0.5 flex-shrink-0 text-emerald-500" />
                          ) : session.latest_status === "failed" ? (
                            <XCircle size={14} className="mt-0.5 flex-shrink-0 text-red-500" />
                          ) : (
                            <Clock size={14} className="mt-0.5 flex-shrink-0 text-blue-500" />
                          )}
                        </div>
                        <div className="line-clamp-2 text-[11px] leading-relaxed text-gray-400">
                          {session.display_preview || session.latest_error_preview || "暂无摘要"}
                        </div>
                        <div className="flex items-center justify-between text-[10px] text-gray-400">
                          <span className="truncate">
                            {formatDistanceToNow((session.latest_updated_at || session.updated_at) * 1000, {
                              addSuffix: true,
                              locale: zhCN
                            })}
                          </span>
                          <span>{session.turn_count} 轮</span>
                        </div>
                      </button>
                    );
                  })}
                  {pagination?.has_more ? (
                    <div className="p-2 text-[10px] text-gray-400">当前仅展示前 50 条会话，分页按钮后续再补。</div>
                  ) : null}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
