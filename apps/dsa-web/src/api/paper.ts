import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  PaperAccountCreateRequest,
  PaperAccountListItem,
  PaperAccountInspect,
  PaperFill,
  PaperOrder,
  PaperProposal,
  PaperPerformanceComparison,
  PaperRiskDecision,
  PaperRun,
} from '../types/paper';

type ProposalListResponse = { items: PaperProposal[] };
type OrderListResponse = { items: PaperOrder[] };
type FillListResponse = { items: PaperFill[] };
type AccountListResponse = { items: PaperAccountListItem[] };
type RunListResponse = { items: PaperRun[]; page: number; pageSize: number; total: number; hasMore: boolean };

function accountPath(accountId: number): string {
  return `/api/v1/paper/accounts/${accountId}`;
}

export const paperApi = {
  async listAccounts(includeInactive = false): Promise<AccountListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/paper/accounts', {
      params: { include_inactive: includeInactive },
    });
    return toCamelCase<AccountListResponse>(response.data);
  },

  async createAccount(payload: PaperAccountCreateRequest): Promise<PaperAccountInspect> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/paper/accounts', {
      name: payload.name.trim(),
      market: payload.market ?? 'cn',
      base_currency: payload.baseCurrency ?? 'CNY',
      controller_kind: payload.controllerKind ?? 'shadow',
      approval_mode: payload.approvalMode ?? 'human_confirm',
      mandate: payload.mandate ?? {},
      mandate_version: payload.mandateVersion ?? '1',
      initial_cash: payload.initialCash ?? 0,
    });
    return toCamelCase<PaperAccountInspect>(response.data);
  },

  async inspect(accountId: number): Promise<PaperAccountInspect> {
    const response = await apiClient.get<Record<string, unknown>>(accountPath(accountId));
    return toCamelCase<PaperAccountInspect>(response.data);
  },

  async getRiskDecision(accountId: number, proposalId: string): Promise<PaperRiskDecision> {
    const response = await apiClient.get<Record<string, unknown>>(
      `${accountPath(accountId)}/proposals/${encodeURIComponent(proposalId)}/risk`,
    );
    return toCamelCase<{ riskDecision: PaperRiskDecision }>(response.data).riskDecision;
  },

  async listRuns(accountId: number): Promise<RunListResponse> {
    const response = await apiClient.get<Record<string, unknown>>(`${accountPath(accountId)}/runs`);
    return toCamelCase<RunListResponse>(response.data);
  },

  async listProposals(accountId: number): Promise<ProposalListResponse> {
    const response = await apiClient.get<Record<string, unknown>>(`${accountPath(accountId)}/proposals`);
    return toCamelCase<ProposalListResponse>(response.data);
  },

  async approveProposal(accountId: number, proposalId: string, approvedBy = 'local_user', expectedVersion?: number): Promise<PaperProposal> {
    void approvedBy; // compatibility only; the server derives the authenticated actor
    const response = await apiClient.post<Record<string, unknown>>(
      `${accountPath(accountId)}/proposals/${encodeURIComponent(proposalId)}/approve`,
      { expected_version: expectedVersion },
    );
    return toCamelCase<{ proposal: PaperProposal }>(response.data).proposal;
  },

  async rejectProposal(accountId: number, proposalId: string, rejectedBy = 'local_user', expectedVersion?: number): Promise<PaperProposal> {
    void rejectedBy; // compatibility only; the server derives the authenticated actor
    const response = await apiClient.post<Record<string, unknown>>(
      `${accountPath(accountId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
      { expected_version: expectedVersion },
    );
    return toCamelCase<{ proposal: PaperProposal }>(response.data).proposal;
  },

  async pause(accountId: number, expectedVersion?: number): Promise<PaperAccountInspect> {
    const response = await apiClient.post<Record<string, unknown>>(`${accountPath(accountId)}/pause`, { expected_version: expectedVersion });
    return toCamelCase<PaperAccountInspect>(response.data);
  },

  async resume(accountId: number, expectedVersion?: number): Promise<PaperAccountInspect> {
    const response = await apiClient.post<Record<string, unknown>>(`${accountPath(accountId)}/resume`, { expected_version: expectedVersion });
    return toCamelCase<PaperAccountInspect>(response.data);
  },

  async freeze(accountId: number, expectedVersion?: number): Promise<PaperAccountInspect> {
    const response = await apiClient.post<Record<string, unknown>>(`${accountPath(accountId)}/freeze`, { expected_version: expectedVersion });
    return toCamelCase<PaperAccountInspect>(response.data);
  },

  async updateMandate(accountId: number, payload: { mandate: Record<string, unknown>; mandateVersion: string; expectedVersion?: number }): Promise<PaperAccountInspect> {
    const response = await apiClient.post<Record<string, unknown>>(`${accountPath(accountId)}/mandate`, {
      mandate: payload.mandate,
      mandate_version: payload.mandateVersion,
      expected_version: payload.expectedVersion,
    });
    return toCamelCase<PaperAccountInspect>(response.data);
  },

  async listOrders(accountId: number): Promise<OrderListResponse> {
    const response = await apiClient.get<Record<string, unknown>>(`${accountPath(accountId)}/orders`);
    return toCamelCase<OrderListResponse>(response.data);
  },

  async listFills(accountId: number): Promise<FillListResponse> {
    const response = await apiClient.get<Record<string, unknown>>(`${accountPath(accountId)}/fills`);
    return toCamelCase<FillListResponse>(response.data);
  },

  async comparePerformance(accountIds: number[], startDate: string, endDate: string, benchmark: Record<string, unknown> = {}): Promise<PaperPerformanceComparison> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/paper/performance/compare', {
      account_ids: accountIds,
      start_date: startDate,
      end_date: endDate,
      benchmark,
    });
    return toCamelCase<PaperPerformanceComparison>(response.data);
  },
};
