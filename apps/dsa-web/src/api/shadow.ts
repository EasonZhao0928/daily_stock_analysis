import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  ShadowApprovalRequest,
  ShadowBacktestRequest,
  ShadowBacktestResponse,
  ShadowProfileCreateRequest,
  ShadowProfileDetail,
  ShadowProfileListResponse,
  ShadowObservation,
  ShadowScanRequest,
  ShadowSignalListResponse,
} from '../types/shadow';

function toSnakeObservation(observation: ShadowObservation): Record<string, unknown> {
  return {
    date: observation.date,
    features: observation.features,
    next_return_pct: observation.nextReturnPct,
    features_as_of: observation.featuresAsOf,
    source_refs: observation.sourceRefs,
    evidence_refs: observation.evidenceRefs,
  };
}

function toSnakeBacktestPayload(payload: ShadowBacktestRequest): Record<string, unknown> {
  const request: Record<string, unknown> = {
    code: payload.code.trim().toUpperCase(),
    split_date: payload.splitDate,
    market_data_start: payload.marketDataStart,
    market_data_end: payload.marketDataEnd,
    fee_bps: payload.feeBps,
    slippage_bps: payload.slippageBps,
    source_refs: payload.sourceRefs,
    account_id: payload.accountId,
    ledger_start_date: payload.ledgerStartDate,
    ledger_end_date: payload.ledgerEndDate,
  };
  if (payload.observations?.length) {
    request.observations = payload.observations.map(toSnakeObservation);
  }
  return request;
}

function toSnakeScanPayload(payload: ShadowScanRequest): Record<string, unknown> {
  const request: Record<string, unknown> = {
    code: payload.code.trim().toUpperCase(),
    market_data_start: payload.marketDataStart,
    market_data_end: payload.marketDataEnd,
    run_id: payload.runId,
    evidence_refs: payload.evidenceRefs,
  };
  if (payload.observations?.length) {
    request.observations = payload.observations.map(toSnakeObservation);
  }
  return request;
}

export const shadowApi = {
  async listProfiles(status?: string): Promise<ShadowProfileListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/shadow/profiles', {
      params: status ? { status } : undefined,
    });
    return toCamelCase<ShadowProfileListResponse>(response.data);
  },

  async createProfile(payload: ShadowProfileCreateRequest): Promise<ShadowProfileDetail> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/shadow/profiles', {
      name: payload.name.trim(),
      rule: payload.rule,
      description: payload.description?.trim() || undefined,
      version: payload.version?.trim() || undefined,
    });
    return toCamelCase<ShadowProfileDetail>(response.data);
  },

  async runBacktest(profileId: string, payload: ShadowBacktestRequest): Promise<ShadowBacktestResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/shadow/profiles/${encodeURIComponent(profileId)}/backtest`,
      toSnakeBacktestPayload(payload),
    );
    return toCamelCase<ShadowBacktestResponse>(response.data);
  },

  async approveProfile(profileId: string, payload: ShadowApprovalRequest = {}): Promise<ShadowProfileDetail> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/shadow/profiles/${encodeURIComponent(profileId)}/approve`,
      { approved_by: payload.approvedBy?.trim() || 'local_user' },
    );
    return toCamelCase<ShadowProfileDetail>(response.data);
  },

  async scanSignals(profileId: string, payload: ShadowScanRequest): Promise<ShadowSignalListResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/shadow/profiles/${encodeURIComponent(profileId)}/scan`,
      toSnakeScanPayload(payload),
    );
    return toCamelCase<ShadowSignalListResponse>(response.data);
  },

  async listSignals(profileId: string, code?: string): Promise<ShadowSignalListResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/shadow/profiles/${encodeURIComponent(profileId)}/signals`,
      { params: code?.trim() ? { code: code.trim().toUpperCase() } : undefined },
    );
    return toCamelCase<ShadowSignalListResponse>(response.data);
  },
};
