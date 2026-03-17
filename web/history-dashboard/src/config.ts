import type { PageConfig } from "./types";

export const pageConfig: PageConfig = window.__HISTORY_PAGE_CONFIG__ || {
  authToken: "",
  initialTurnLimit: 50,
  initialProject: "",
  initialChatId: ""
};
