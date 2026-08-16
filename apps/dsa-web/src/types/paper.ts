/** Virtual Paper Account workbench contracts. */

export type PaperState = 'active' | 'paused' | 'frozen' | 'closed' | string;

export interface PaperAccount {
  id: number;
  name: string;
  broker?: string | null;
  market: string;
  baseCurrency: string;
  isActive: boolean;
  accountKind?: string;
  controllerKind?: string;
  externalExecutionEnabled?: boolean;
}

export interface PaperConfig {
  configId: string;
  accountId: number;
  configVersion: number;
  controllerKind: string;
  approvalMode: string;
  mandateVersion: string;
  mandate: Record<string, unknown>;
  initialCash: number;
  state: PaperState;
  enabled: boolean;
}

export interface PaperProposal {
  proposalId: string;
  runId?: string | null;
  accountId: number;
  proposalVersion: string;
  symbol: string;
  market: string;
  side: string;
  orderType: string;
  quantity?: number | null;
  targetWeight?: number | null;
  limitPrice?: number | null;
  stopPrice?: number | null;
  rationale?: string | null;
  evidenceRefs: Array<Record<string, unknown>>;
  proposalHash: string;
  status: string;
  decisionBy?: string | null;
  decisionAt?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface PaperAccountInspect {
  account: PaperAccount;
  config: PaperConfig;
  proposals: PaperProposal[];
}

export interface PaperAccountListItem {
  account: PaperAccount;
  config: PaperConfig;
}

export interface PaperRiskDecision {
  riskDecisionId: string;
  proposalId: string;
  runId: string;
  decision: string;
  ruleCodes: string[];
  details: Record<string, unknown>;
  mandateVersion: string;
  createdAt?: string | null;
}

export interface PaperPerformanceComparison {
  startDate: string;
  endDate: string;
  items: Array<Record<string, unknown>>;
  benchmark?: Record<string, unknown>;
  limitations?: string[];
}

export interface PaperRun {
  runId: string;
  accountId: number;
  decisionAt: string;
  strategyVersion: string;
  status: string;
  backend?: string | null;
  model?: string | null;
  diagnostics: Record<string, unknown>;
}

export interface PaperAccountCreateRequest {
  name: string;
  market?: string;
  baseCurrency?: string;
  controllerKind?: string;
  approvalMode?: string;
  mandate?: Record<string, unknown>;
  mandateVersion?: string;
  initialCash?: number;
}

export interface PaperOrder {
  orderId: string;
  proposalId: string;
  accountId: number;
  symbol: string;
  market: string;
  side: string;
  orderType: string;
  quantity: number;
  limitPrice?: number | null;
  stopPrice?: number | null;
  status: string;
  version: number;
  observationId?: string | null;
  observationCutoff?: string | null;
  executionPolicy: string;
  expiresAt?: string | null;
  filledQuantity: number;
  avgFillPrice?: number | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface PaperFill {
  fillId: string;
  orderId: string;
  accountId: number;
  fillDate: string;
  quantity: number;
  price: number;
  fee: number;
  tax: number;
  fillHash: string;
  status: string;
  ledgerOutboxId?: string | null;
  barTimestamp?: string | null;
  executionPolicy: string;
  createdAt?: string | null;
}
