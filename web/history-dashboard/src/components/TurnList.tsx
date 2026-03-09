import React, { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { format } from "date-fns";
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Terminal,
  User
} from "lucide-react";

import { api } from "../api";
import { pageConfig } from "../config";
import type { Session, Turn } from "../types";

export function TurnList({ session, onBack }: { session: Session; onBack: () => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const turnLimit = Math.max(20, Math.min(100, Number(pageConfig.initialTurnLimit || 50)));

  useEffect(() => {
    void loadTurns(false);
  }, [session.chat_id, session.project]);

  async function loadTurns(loadOlder: boolean) {
    const baseOffset = Math.max(0, Number(session.turn_count || 0) - turnLimit);
    const nextOffset = loadOlder ? Math.max(0, offset - turnLimit) : baseOffset;

    if (loadOlder) {
      setLoadingOlder(true);
    } else {
      setLoading(true);
    }
    setError("");

    try {
      const response = await api.getTurns(session.project, session.chat_id, nextOffset, turnLimit);
      const turnItems = response.data.turns;
      setTurns((prev) => (loadOlder ? turnItems.concat(prev) : turnItems));
      setOffset(response.data.pagination.offset);
      setTotal(response.data.pagination.total);

      if (!loadOlder) {
        setTimeout(() => {
          if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
          }
        }, 100);
      }
    } catch (err) {
      setError(String((err as Error).message || err));
    } finally {
      setLoading(false);
      setLoadingOlder(false);
    }
  }

  return (
    <div className="flex h-full flex-col bg-gray-50/50">
      <div className="sticky top-0 z-10 flex items-center gap-3 border-b border-gray-200 bg-white p-4 shadow-sm">
        <div className="min-w-0 flex-1">
          <nav className="mb-1.5 flex items-center space-x-1.5 text-xs font-medium text-gray-500">
            <button onClick={onBack} className="-ml-1 flex items-center gap-1 rounded px-1 transition-colors hover:text-gray-900">
              <ChevronLeft size={14} className="md:hidden" />
              {session.project}
            </button>
            <ChevronRight size={14} className="text-gray-400" />
            <span className="truncate text-gray-900">{session.display_title}</span>
          </nav>
          <div className="flex items-center gap-2 text-xs text-gray-500">
            <span className="rounded bg-gray-100 px-1.5 py-0.5 font-medium text-gray-600">{session.model}</span>
            <span className="truncate text-gray-400">{session.cwd}</span>
          </div>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto scroll-smooth p-4 md:p-6">
        {session.turn_count > turns.length && offset > 0 ? (
          <div className="flex justify-center">
            <button
              onClick={() => void loadTurns(true)}
              className="rounded-full border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-500 shadow-sm transition-colors hover:bg-gray-50"
              disabled={loadingOlder}
            >
              {loadingOlder ? "加载中..." : `加载更早轮次（当前 ${turns.length}/${total}）`}
            </button>
          </div>
        ) : null}

        {loading ? (
          <div className="flex h-full items-center justify-center text-sm text-gray-400">加载历史记录中...</div>
        ) : error ? (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</div>
        ) : turns.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-gray-400">暂无对话记录。</div>
        ) : (
          turns.map((turn, idx) => <TurnItem key={turn.id} turn={turn} isLast={idx === turns.length - 1} />)
        )}
      </div>
    </div>
  );
}

function TurnItem({ turn }: { turn: Turn; isLast: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [details, setDetails] = useState<Turn | null>(null);
  const [loadingDetails, setLoadingDetails] = useState(false);

  const handleToggleExpand = async () => {
    if (!expanded && !details) {
      setLoadingDetails(true);
      try {
        const response = await api.getTurn(turn.turn_id || turn.id);
        setDetails(response.data.turn);
      } finally {
        setLoadingDetails(false);
      }
    }
    setExpanded((value) => !value);
  };

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-5">
      <div className="flex justify-end gap-3">
        <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-gray-900 px-4 py-3 text-sm leading-relaxed text-white shadow-sm">
          {turn.user_text}
        </div>
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border border-gray-300 bg-gray-200 shadow-sm">
          <User size={16} className="text-gray-600" />
        </div>
      </div>

      <div className="flex gap-3">
        <div className="mt-1 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border border-indigo-700 bg-indigo-600 shadow-sm">
          <Bot size={16} className="text-white" />
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <div className="flex items-center gap-3 text-xs text-gray-500">
            <span className="font-semibold text-gray-700">FeiCodex</span>
            <span>{turn.started_at ? format(turn.started_at * 1000, "yyyy/MM/dd HH:mm") : "未知时间"}</span>
            {turn.duration_sec > 0 ? (
              <span className="flex items-center gap-1 text-gray-400">
                <Clock size={12} />
                {turn.duration_sec}秒
              </span>
            ) : null}
            {turn.status === "failed" ? (
              <span className="flex items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 font-medium text-red-600">
                <AlertCircle size={12} />
                失败
              </span>
            ) : null}
            {turn.status === "completed" ? (
              <span className="flex items-center gap-1 rounded bg-emerald-50 px-1.5 py-0.5 font-medium text-emerald-600">
                <CheckCircle2 size={12} />
                完成
              </span>
            ) : null}
          </div>

          {(turn.events_count > 0 || turn.error_text) ? (
            <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
              <button
                onClick={() => void handleToggleExpand()}
                className="flex w-full items-center justify-between p-3 text-xs text-gray-600 transition-colors hover:bg-gray-50"
              >
                <div className="flex items-center gap-2 font-medium">
                  <Terminal size={14} className="text-gray-400" />
                  <span>{turn.events_count} 条过程记录</span>
                </div>
                {expanded ? <ChevronDown size={14} className="text-gray-400" /> : <ChevronRight size={14} className="text-gray-400" />}
              </button>

              {expanded ? (
                <div className="max-h-80 space-y-2 overflow-y-auto border-t border-gray-200 bg-gray-900 p-4 font-mono text-[11px] text-gray-300">
                  {loadingDetails ? (
                    <div className="flex items-center gap-2 text-gray-500">
                      <div className="h-3 w-3 animate-spin rounded-full border-2 border-gray-500 border-t-transparent" />
                      加载记录中...
                    </div>
                  ) : details?.events?.length ? (
                    details.events.map((eventItem, index) => (
                      <div key={index} className="-mx-1 flex gap-3 rounded p-1 hover:bg-gray-800/50">
                        <span className="select-none text-gray-500">{format(eventItem.ts * 1000, "HH:mm:ss")}</span>
                        <span className="break-words text-gray-200">{eventItem.text}</span>
                      </div>
                    ))
                  ) : (
                    <div className="text-gray-500">暂无过程记录。</div>
                  )}
                </div>
              ) : null}
            </div>
          ) : null}

          {turn.error_text ? (
            <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800 shadow-sm">
              <div className="flex items-start gap-2">
                <AlertCircle size={16} className="mt-0.5 flex-shrink-0 text-red-600" />
                <div className="font-mono text-xs">{turn.error_text}</div>
              </div>
            </div>
          ) : null}

          {turn.assistant_text ? (
            <div className="prose prose-sm prose-gray max-w-none rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
              <ReactMarkdown>{turn.assistant_text}</ReactMarkdown>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
