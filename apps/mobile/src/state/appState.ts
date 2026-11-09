import type { Domain, Project, SourceDetail, SourceSummary } from "../types/domain";

export type View = { name: "inbox" } | { name: "capture" } | { name: "detail"; sourceId: string };

export interface AppState {
  view: View;
  domains: Domain[];
  projects: Project[];
  sources: SourceSummary[];
  selectedSource?: SourceDetail | undefined;
  loading: boolean;
  refreshing: boolean;
  error?: string | undefined;
}

export type AppAction =
  | { type: "bootstrapStarted" }
  | { type: "bootstrapSucceeded"; domains: Domain[]; projects: Project[]; sources: SourceSummary[] }
  | { type: "requestFailed"; message: string }
  | { type: "refreshStarted" }
  | { type: "sourcesLoaded"; sources: SourceSummary[] }
  | { type: "openCapture" }
  | { type: "openInbox" }
  | { type: "detailStarted"; sourceId: string }
  | { type: "detailLoaded"; source: SourceDetail }
  | { type: "sourceSaved"; source: SourceDetail };

export const initialState: AppState = {
  view: { name: "inbox" },
  domains: [],
  projects: [],
  sources: [],
  loading: true,
  refreshing: false,
};

export const appReducer = (state: AppState, action: AppAction): AppState => {
  switch (action.type) {
    case "bootstrapStarted":
      return { ...state, loading: true, error: undefined };
    case "bootstrapSucceeded":
      return {
        ...state,
        domains: action.domains,
        projects: action.projects,
        sources: action.sources,
        loading: false,
        error: undefined,
      };
    case "requestFailed":
      return { ...state, loading: false, refreshing: false, error: action.message };
    case "refreshStarted":
      return { ...state, refreshing: true, error: undefined };
    case "sourcesLoaded":
      return { ...state, sources: action.sources, refreshing: false, error: undefined };
    case "openCapture":
      return { ...state, view: { name: "capture" }, error: undefined };
    case "openInbox":
      return { ...state, view: { name: "inbox" }, selectedSource: undefined, error: undefined };
    case "detailStarted":
      return { ...state, view: { name: "detail", sourceId: action.sourceId }, loading: true, error: undefined };
    case "detailLoaded":
      return { ...state, selectedSource: action.source, loading: false, error: undefined };
    case "sourceSaved": {
      const summary: SourceSummary = action.source;
      const sources = [summary, ...state.sources.filter((item) => item.id !== summary.id)];
      return {
        ...state,
        sources,
        selectedSource: action.source,
        view: { name: "detail", sourceId: action.source.id },
        loading: false,
        error: undefined,
      };
    }
  }
};
