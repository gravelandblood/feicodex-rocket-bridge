import React, { useState } from "react";
import { TerminalSquare } from "lucide-react";

import { Sidebar } from "./components/Sidebar";
import { TurnList } from "./components/TurnList";
import type { Session } from "./types";

export default function App() {
  const [selectedSession, setSelectedSession] = useState<Session | null>(null);

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50 font-sans text-gray-900 selection:bg-indigo-100 selection:text-indigo-900">
      <div
        className={`z-30 flex w-full flex-shrink-0 flex-col border-r border-gray-200 bg-white shadow-sm md:w-80 lg:w-96 ${
          selectedSession ? "hidden md:flex" : "flex"
        }`}
      >
        <Sidebar selectedSession={selectedSession} onSelectSession={setSelectedSession} />
      </div>

      <div className={`z-10 flex min-w-0 flex-1 flex-col bg-gray-50 ${!selectedSession ? "hidden md:flex" : "flex"}`}>
        {selectedSession ? (
          <TurnList session={selectedSession} onBack={() => setSelectedSession(null)} />
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 bg-gray-50/50 p-8 text-center text-gray-400">
            <TerminalSquare size={32} className="opacity-40" />
            <p className="text-sm">请在左侧选择一个会话以查看对话历史</p>
          </div>
        )}
      </div>
    </div>
  );
}
