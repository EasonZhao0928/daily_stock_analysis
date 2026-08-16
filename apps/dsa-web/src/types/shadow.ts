/**
 * Shadow Research API contracts.
 *
 * Shadow Research is deliberately limited to deterministic research signals;
 * these types do not represent broker orders or live account mutations.
 */

export type ShadowProfileStatus = 'draft' | 'degraded' | 'approved' | 'disabled' | 'frozen';

export interface ShadowRule {
  ruleId: number;
  profileId: string;
  version: string;
  dsl: Record<string, unknown>;
  ruleHash: string;
  featureNames: string[];
  status: string;
  createdAt?: string | null;
}

export interface ShadowBacktestRun {
  runId: string;
  profileId: string;
  ruleId: number;
  code: string;
  splitDate: string;
  sourceSnapshotHash: string;
  status: 'completed' | 'degraded' | string;
  metrics: Record<string, unknown>;
  degradedReason?: string | null;
  createdAt?: string | null;
}

export interface ShadowProfile {
  profileId: string;
  name: string;
  description?: string | null;
  status: ShadowProfileStatus | string;
  ruleVersion: string;
  approvedAt?: string | null;
  approvedBy?: string | null;
  degradedReason?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface ShadowProfileDetail {
  profile: ShadowProfile;
  rule: ShadowRule | null;
  latestRun: ShadowBacktestRun | null;
}

export interface ShadowProfileListResponse {
  items: ShadowProfileDetail[];
}

export interface ShadowProfileCreateRequest {
  name: string;
  rule: Record<string, unknown>;
  description?: string;
  version?: string;
}

export interface ShadowObservation {
  date: string;
  features: Record<string, unknown>;
  nextReturnPct?: number | null;
  featuresAsOf?: string;
  sourceRefs?: Array<Record<string, unknown>>;
  evidenceRefs?: Array<Record<string, unknown>>;
}

export interface ShadowBacktestRequest {
  code: string;
  observations?: ShadowObservation[];
  splitDate: string;
  marketDataStart?: string;
  marketDataEnd?: string;
  feeBps?: number;
  slippageBps?: number;
  sourceRefs?: Array<Record<string, unknown>>;
  accountId?: number;
  ledgerStartDate?: string;
  ledgerEndDate?: string;
}

export interface ShadowBacktestResponse {
  run: ShadowBacktestRun;
  metrics: Record<string, unknown>;
}

export interface ShadowApprovalRequest {
  approvedBy?: string;
}

export interface ShadowScanRequest {
  code: string;
  observations?: ShadowObservation[];
  marketDataStart?: string;
  marketDataEnd?: string;
  runId?: string;
  evidenceRefs?: Array<Record<string, unknown>>;
}

export interface ShadowSignal {
  signalId: string;
  profileId: string;
  ruleId: number;
  runId: string;
  code: string;
  signalDate: string;
  status: string;
  featureSnapshotHash: string;
  dataCutoff: string;
  evidenceRefs: Array<Record<string, unknown>>;
  reason?: string | null;
  createdAt?: string | null;
}

export interface ShadowSignalListResponse {
  items: ShadowSignal[];
}
