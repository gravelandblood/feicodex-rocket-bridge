export interface Pagination {
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
}

export interface Project {
  name: string;
  started_at: number;
  updated_at: number;
  turn_count: number;
  session_count: number;
}

export interface Session {
  project: string;
  chat_id: string;
  cwd: string;
  model: string;
  auth_profile: string;
  started_at: number;
  updated_at: number;
  turn_count: number;
  latest_turn_id: string;
  latest_status: string;
  latest_started_at: number;
  latest_ended_at: number;
  latest_updated_at: number;
  latest_user_text: string;
  latest_user_preview: string;
  latest_assistant_preview: string;
  latest_error_preview: string;
  display_title: string;
  display_preview: string;
}

export interface TurnEvent {
  ts: number;
  text: string;
}

export interface Turn {
  id: string;
  project: string;
  chat_id: string;
  thread_id?: string;
  turn_id: string;
  cwd?: string;
  model?: string;
  auth_profile?: string;
  status: string;
  started_at: number;
  ended_at: number;
  duration_sec: number;
  user_text: string;
  assistant_text: string;
  error_text: string;
  events_count: number;
  events?: TurnEvent[];
}

export interface PageConfig {
  authToken: string;
  initialTurnLimit: number;
  initialProject: string;
  initialChatId: string;
}

export interface HistoryEnvelope<T> {
  ok: boolean;
  data: T;
}

declare global {
  interface Window {
    __HISTORY_PAGE_CONFIG__?: PageConfig;
  }
}
