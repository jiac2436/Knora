import { environment } from "../config/env";
import type {
  CaptureInput,
  ContextUpdate,
  Domain,
  ExperienceInput,
  Project,
  SourceDetail,
  SourceSummary,
} from "../types/domain";

interface ApiErrorBody {
  code?: string;
  message?: string;
  request_id?: string;
  retryable?: boolean;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly retryable: boolean,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const request = async <T>(
  path: string,
  init: RequestInit = {},
  extraHeaders: Record<string, string> = {},
): Promise<T> => {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "Content-Type": "application/json",
    ...extraHeaders,
  };

  if (environment.devBearerToken) {
    headers.Authorization = `Bearer ${environment.devBearerToken}`;
  }

  const response = await fetch(`${environment.apiBaseUrl}${path}`, {
    ...init,
    headers: { ...headers, ...init.headers },
  });

  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // 非 JSON 错误统一为可读的 HTTP 状态，避免暴露服务端响应正文。
    }
    throw new ApiError(
      body.message ?? `请求失败（HTTP ${response.status}）`,
      response.status,
      body.code ?? "http_error",
      body.retryable ?? response.status >= 500,
      body.request_id,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
};

interface Page<T> {
  items: T[];
  nextCursor?: string | null;
}

type WireRecord = Record<string, unknown>;

const stringValue = (value: unknown, fallback = ""): string =>
  typeof value === "string" ? value : fallback;

const stringList = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

const asRecord = (value: unknown): WireRecord =>
  typeof value === "object" && value !== null ? (value as WireRecord) : {};

const mapDomain = (value: unknown): Domain => {
  const item = asRecord(value);
  return { id: stringValue(item.id), name: stringValue(item.name, "未命名知识域") };
};

const mapProject = (value: unknown): Project => {
  const item = asRecord(value);
  const domainId = stringValue(item.domain_id ?? item.domainId);
  return {
    id: stringValue(item.id),
    name: stringValue(item.name, "未命名项目"),
    ...(domainId ? { domainId } : {}),
  };
};

const mapSource = (value: unknown): SourceDetail => {
  const item = asRecord(value);
  const context = asRecord(item.context ?? item.personal_context ?? item.personalContext);
  const rawExperiences = Array.isArray(item.experiences) ? item.experiences : [];
  const rawProjectIds = item.project_ids ?? item.projectIds;
  const projectIds = stringList(rawProjectIds);
  const singleProjectId = stringValue(item.project_id ?? item.projectId);
  if (singleProjectId && !projectIds.includes(singleProjectId)) projectIds.push(singleProjectId);

  const latestResult = rawExperiences.length
    ? stringValue(asRecord(rawExperiences[0]).result, "untried")
    : stringValue(item.latest_verification ?? item.latestVerification, "untried");

  return {
    id: stringValue(item.id),
    title: stringValue(item.title, "未命名资料"),
    kind: stringValue(item.kind ?? item.capture_type, "text") as SourceDetail["kind"],
    ...(stringValue(item.raw_url ?? item.source_url ?? item.rawUrl)
      ? { rawUrl: stringValue(item.raw_url ?? item.source_url ?? item.rawUrl) }
      : {}),
    domainId: stringValue(item.domain_id ?? item.domainId),
    projectIds,
    uploadStatus: stringValue(item.upload_status ?? item.uploadStatus, "saved_cloud") as SourceDetail["uploadStatus"],
    processingStatus: stringValue(item.processing_status ?? item.processingStatus, "queued") as SourceDetail["processingStatus"],
    coverage: stringValue(item.coverage, "link_only") as SourceDetail["coverage"],
    visibility: stringValue(item.visibility, "app_and_agents") as SourceDetail["visibility"],
    personalContext: {
      reason: stringValue(context.reason ?? context.save_reason),
      notes: stringValue(context.notes ?? context.note),
      applicability: stringValue(context.applicability),
      version: Number(context.version ?? 1),
    },
    latestVerification: latestResult as SourceDetail["latestVerification"],
    createdAt: stringValue(item.created_at ?? item.createdAt, new Date().toISOString()),
    updatedAt: stringValue(item.updated_at ?? item.updatedAt, new Date().toISOString()),
    rawInput: stringValue(item.raw_input ?? item.rawInput ?? item.content),
    extractedText: stringValue(item.extracted_text ?? item.extractedText ?? item.content),
    experiences: rawExperiences.map((entry, index) => {
      const experience = asRecord(entry);
      const projectId = stringValue(experience.project_id ?? experience.projectId);
      return {
        id: stringValue(experience.id, String(index)),
        ...(projectId ? { projectId } : {}),
        result: stringValue(experience.result, "untried") as SourceDetail["latestVerification"],
        notes: stringValue(experience.note ?? experience.notes),
        verifiedAt: stringValue(experience.verified_at ?? experience.verifiedAt, new Date().toISOString()),
      };
    }),
    ...(stringValue(item.etag) ? { etag: stringValue(item.etag) } : {}),
  };
};

const mapPage = <T>(value: unknown, mapper: (item: unknown) => T): Page<T> => {
  const page = asRecord(value);
  return {
    items: Array.isArray(page.items) ? page.items.map(mapper) : [],
    ...(typeof page.next_cursor === "string" ? { nextCursor: page.next_cursor } : {}),
  };
};

export const api = {
  listDomains: async () => mapPage(await request<unknown>("/domains"), mapDomain),
  listProjects: async () => mapPage(await request<unknown>("/projects"), mapProject),
  listSources: async () => mapPage<SourceSummary>(await request<unknown>("/sources?limit=50"), mapSource),
  getSource: async (sourceId: string) =>
    mapSource(await request<unknown>(`/sources/${encodeURIComponent(sourceId)}`)),
  createCapture: async (input: CaptureInput) =>
    mapSource(await request<unknown>(
      "/captures",
      {
        method: "POST",
        body: JSON.stringify({
          capture_type: input.kind === "link" ? "url" : "text",
          ...(input.title ? { title: input.title } : {}),
          ...(input.kind === "link" ? { source_url: input.rawInput } : {}),
          captured_at: new Date().toISOString(),
          content: { text: input.kind === "text" ? input.rawInput : "" },
          coverage: input.kind === "link" ? "link_only" : "full_text",
          context: {
            domain_id: input.domainId,
            ...(input.projectId ? { project_id: input.projectId } : {}),
            ...(input.notes ? { note: input.notes } : {}),
            ...(input.reason ? { save_reason: input.reason } : {}),
          },
          client: { kind: "mobile", version: "0.1.0" },
        }),
      },
      { "Idempotency-Key": input.clientRequestId },
    )),
  updateContext: async (sourceId: string, input: ContextUpdate, etag?: string) =>
    mapSource(await request<unknown>(
      `/sources/${encodeURIComponent(sourceId)}/context`,
      {
        method: "PATCH",
        body: JSON.stringify({
          domain_id: input.domainId,
          project_ids: input.projectIds,
          visibility: input.visibility,
          save_reason: input.reason,
          note: input.notes,
          applicability: input.applicability,
          version: input.version,
        }),
      },
      etag ? { "If-Match": etag } : {},
    )),
  addExperience: async (sourceId: string, input: ExperienceInput) => {
    await request<unknown>(
      `/sources/${encodeURIComponent(sourceId)}/experiences`,
      {
        method: "POST",
        body: JSON.stringify({
          result: input.result,
          notes: input.notes,
          ...(input.projectId ? { project_id: input.projectId } : {}),
        }),
      },
    );
    return api.getSource(sourceId);
  },
};

export const describeError = (error: unknown): string => {
  if (error instanceof ApiError) {
    const retry = error.retryable ? "，可以稍后重试" : "";
    return `${error.message}${retry}`;
  }
  if (error instanceof Error && error.message.toLowerCase().includes("network")) {
    return "无法连接服务器，请检查网络和 API 地址。";
  }
  return "操作未完成，请稍后重试。";
};
