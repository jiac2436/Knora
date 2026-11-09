export type UploadStatus =
  | "saved_local"
  | "uploading"
  | "saved_cloud"
  | "upload_failed";

export type ProcessingStatus =
  | "queued"
  | "parsing"
  | "organizing"
  | "ready"
  | "needs_input"
  | "failed";

export type Coverage = "link_only" | "partial_text" | "full_text";
export type VerificationResult =
  | "untried"
  | "effective"
  | "partially_effective"
  | "ineffective";

export type Visibility = "app_and_agents" | "app_only";

export interface Domain {
  id: string;
  name: string;
}

export interface Project {
  id: string;
  name: string;
  domainId?: string;
}

export interface PersonalContext {
  reason: string;
  notes: string;
  applicability: string;
  version: number;
}

export interface ExperienceRecord {
  id: string;
  projectId?: string;
  result: VerificationResult;
  notes: string;
  verifiedAt: string;
}

export interface SourceSummary {
  id: string;
  title: string;
  kind: "text" | "link" | "browser_clip";
  rawUrl?: string;
  domainId: string;
  projectIds: string[];
  uploadStatus: UploadStatus;
  processingStatus: ProcessingStatus;
  coverage: Coverage;
  visibility: Visibility;
  personalContext: PersonalContext;
  latestVerification: VerificationResult;
  createdAt: string;
  updatedAt: string;
}

export interface SourceDetail extends SourceSummary {
  rawInput: string;
  extractedText: string;
  experiences: ExperienceRecord[];
  etag?: string;
}

export interface CaptureInput {
  kind: "text" | "link";
  rawInput: string;
  title?: string;
  domainId: string;
  projectId?: string;
  visibility: Visibility;
  reason: string;
  notes: string;
  clientRequestId: string;
}

export interface ContextUpdate {
  domainId: string;
  projectIds: string[];
  visibility: Visibility;
  reason: string;
  notes: string;
  applicability: string;
  version: number;
}

export interface ExperienceInput {
  projectId?: string;
  result: Exclude<VerificationResult, "untried">;
  notes: string;
}
