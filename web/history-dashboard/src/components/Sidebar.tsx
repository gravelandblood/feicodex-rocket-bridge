import React, { useEffect, useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Clock, Folder, LogOut, RefreshCw, Shield, XCircle } from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";

import { api } from "../api";
import { pageConfig } from "../config";
import type { AuthProfile, Pagination, Project, Session } from "../types";

type SessionBag = {
  items: Session[];
  pagination: Pagination | null;
};

function fallbackDefaultProfile(previous?: AuthProfile): AuthProfile {
  return {
    profile: "",
    label: "default",
    email: "",
    valid: true,
    reason: "",
    home_dir: "",
    source_auth_json: "",
    status: "active",
    disabled_until: 0,
    disabled_reason: "",
    needs_reauth: false,
    risk_deactivated: false,
    last_health_check_at: Number(previous?.last_health_check_at || 0),
    last_health_error: String(previous?.last_health_error || ""),
    available: true,
    disabled_remaining_sec: 0
  };
}

function ensureDefaultProfile(items: AuthProfile[], previous?: AuthProfile): AuthProfile[] {
  const normalized = Array.isArray(items) ? [...items] : [];
  if (normalized.some((item) => (item.profile || "") === "")) {
    return normalized;
  }
  return [fallbackDefaultProfile(previous), ...normalized];
}

function statusLabel(item: AuthProfile) {
  const status = String(item.status || "").toLowerCase();
  if (status === "active") return "可用";
  if (status === "temp_disabled") return "临时禁用";
  if (status === "needs_reauth") return "需重登";
  if (status === "deactivated") return "已停用";
  if (status === "invalid") return "无效";
  return status || "未知";
}

function statusTone(item: AuthProfile) {
  const status = String(item.status || "").toLowerCase();
  if (status === "active") return "bg-emerald-50 text-emerald-600";
  if (status === "temp_disabled") return "bg-amber-50 text-amber-700";
  if (status === "needs_reauth" || status === "deactivated") return "bg-red-50 text-red-600";
  return "bg-gray-100 text-gray-600";
}

export function Sidebar({
  selectedSession,
  onSelectSession
}: {
  selectedSession: Session | null;
  onSelectSession: (session: Session) => void;
}) {
  const [activeTab, setActiveTab] = useState<"sessions" | "accounts">("sessions");
  const [projects, setProjects] = useState<Project[]>([]);
  const [sessionsByProject, setSessionsByProject] = useState<Record<string, SessionBag>>({});
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [loadingSessions, setLoadingSessions] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [profiles, setProfiles] = useState<AuthProfile[]>([]);
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [profilesError, setProfilesError] = useState("");
  const [profilesBusy, setProfilesBusy] = useState("");
  const [profilesMessage, setProfilesMessage] = useState("");

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

  useEffect(() => {
    if (activeTab === "accounts") {
      void loadAuthProfiles(false);
    }
  }, [activeTab]);

  const loadAuthProfiles = async (healthCheck: boolean) => {
    setProfilesError("");
    setProfilesLoading(true);
    try {
      const response = healthCheck ? await api.healthCheckAuthProfile("") : await api.getAuthProfiles();
      setProfiles((prev) => {
        const previousDefault = prev.find((item) => (item.profile || "") === "");
        return ensureDefaultProfile(response.data.profiles || [], previousDefault);
      });
      setProfilesMessage(healthCheck ? "健康检测完成" : "");
    } catch (err) {
      setProfilesError(String((err as Error).message || err));
    } finally {
      setProfilesLoading(false);
    }
  };

  const switchProfile = async (profile: string) => {
    if (!selectedSession) {
      setProfilesError("请先在会话列表中选中一个会话，再执行账号切换。");
      return;
    }
    setProfilesBusy(profile || "__default__");
    setProfilesMessage("");
    setProfilesError("");
    try {
      const response = await api.switchAuthProfile(selectedSession.project, selectedSession.chat_id, profile);
      const nextProfile = String(response.data.auth_profile || "");
      onSelectSession({ ...selectedSession, auth_profile: nextProfile });
      setProfilesMessage(`已切换到账号：${nextProfile || "default"}`);
      const refreshed = await api.getAuthProfiles();
      setProfiles((prev) => {
        const previousDefault = prev.find((item) => (item.profile || "") === "");
        return ensureDefaultProfile(refreshed.data.profiles || [], previousDefault);
      });
    } catch (err) {
      setProfilesError(String((err as Error).message || err));
    } finally {
      setProfilesBusy("");
    }
  };

  const healthCheckSingle = async (profile: string) => {
    if (!profile) {
      setProfilesError("");
      setProfilesBusy("");
      setProfilesMessage("default 账号仅支持全量健康检测，请使用上方“健康检测”按钮。");
      return;
    }
    setProfilesError("");
    setProfilesMessage("");
    setProfilesBusy(profile);
    try {
      const response = await api.healthCheckAuthProfile(profile);
      const map = new Map((response.data.profiles || []).map((item) => [item.profile || "", item]));
      setProfiles((prev) => prev.map((item) => map.get(item.profile || "") || item));
      setProfilesMessage(`账号 ${profile} 检测完成`);
    } catch (err) {
      setProfilesError(String((err as Error).message || err));
    } finally {
      setProfilesBusy("");
    }
  };

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
        <div className="mt-3 flex items-center gap-1 rounded-lg bg-gray-100 p-1 text-xs">
          <button
            onClick={() => setActiveTab("sessions")}
            className={`flex-1 rounded-md px-2 py-1.5 transition-colors ${
              activeTab === "sessions" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            会话
          </button>
          <button
            onClick={() => setActiveTab("accounts")}
            className={`flex-1 rounded-md px-2 py-1.5 transition-colors ${
              activeTab === "accounts" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            账号状态
          </button>
        </div>
        {activeTab === "sessions" && error ? <div className="mt-3 rounded-lg bg-red-50 p-3 text-xs text-red-600">{error}</div> : null}
      </div>

      {activeTab === "sessions" ? (
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
      ) : (
        <div className="flex-1 space-y-2 overflow-y-auto p-3">
          <div className="rounded-lg border border-gray-200 bg-white p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="text-xs text-gray-600">
                当前会话：{selectedSession ? `${selectedSession.project} / ${selectedSession.chat_id}` : "未选择"}
              </div>
              <button
                onClick={() => void loadAuthProfiles(true)}
                disabled={profilesLoading}
                className="inline-flex items-center gap-1 rounded border border-gray-200 bg-gray-50 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-50"
              >
                <RefreshCw size={12} className={profilesLoading ? "animate-spin" : ""} />
                健康检测
              </button>
            </div>
            {profilesError ? <div className="mt-2 rounded bg-red-50 p-2 text-xs text-red-600">{profilesError}</div> : null}
            {profilesMessage ? <div className="mt-2 rounded bg-emerald-50 p-2 text-xs text-emerald-600">{profilesMessage}</div> : null}
          </div>

          {profilesLoading ? <div className="p-2 text-xs text-gray-400">账号状态加载中...</div> : null}
          {!profilesLoading && profiles.length === 0 ? <div className="p-2 text-xs text-gray-400">暂无账号数据</div> : null}

          {profiles.map((item) => {
            const isCurrent = (selectedSession?.auth_profile || "") === (item.profile || "");
            const remaining = Number(item.disabled_remaining_sec || 0);
            return (
              <div key={item.profile || "__default__"} className="rounded-lg border border-gray-200 bg-white p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-gray-900">{item.profile || "default"}</div>
                    <div className="truncate text-[11px] text-gray-500">{item.email || item.source_auth_json || "默认账号"}</div>
                  </div>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${statusTone(item)}`}>{statusLabel(item)}</span>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
                  {isCurrent ? <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-700">当前会话使用中</span> : null}
                  {remaining > 0 ? <span>解禁倒计时 {Math.ceil(remaining / 60)} 分钟</span> : null}
                  {item.needs_reauth ? <span>需要替换 auth.json</span> : null}
                  {item.risk_deactivated ? <span>疑似风控停用</span> : null}
                </div>
                {item.disabled_reason || item.reason || item.last_health_error ? (
                  <div className="mt-2 rounded bg-gray-50 p-2 text-[11px] leading-relaxed text-gray-600">
                    {item.disabled_reason || item.reason || item.last_health_error}
                  </div>
                ) : null}
                <div className="mt-2 flex items-center gap-2">
                  <button
                    onClick={() => void healthCheckSingle(item.profile)}
                    disabled={profilesBusy === (item.profile || "__default__") || !item.profile}
                    className="inline-flex items-center gap-1 rounded border border-gray-200 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-40"
                  >
                    <Shield size={12} />
                    {!item.profile ? "仅全量检测" : profilesBusy === (item.profile || "__default__") ? "检测中..." : "检测"}
                  </button>
                  <button
                    onClick={() => void switchProfile(item.profile)}
                    disabled={profilesBusy === (item.profile || "__default__") || !selectedSession || !item.available}
                    className="rounded border border-indigo-200 bg-indigo-50 px-2 py-1 text-[11px] text-indigo-700 hover:bg-indigo-100 disabled:opacity-40"
                  >
                    {profilesBusy === (item.profile || "__default__") ? "切换中..." : "切换到此账号"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
