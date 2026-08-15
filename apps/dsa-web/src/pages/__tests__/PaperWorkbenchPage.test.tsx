import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PaperWorkbenchPage from '../PaperWorkbenchPage';

const {
  getCandles,
  getAnnotations,
  inspect,
  listOrders,
  listFills,
  createAccount,
  approveProposal,
  rejectProposal,
  listAccounts,
  getRiskDecision,
  updateMandate,
  comparePerformance,
  listRuns,
  pause,
  resume,
  freeze,
} = vi.hoisted(() => ({
  getCandles: vi.fn(),
  getAnnotations: vi.fn(),
  inspect: vi.fn(),
  listOrders: vi.fn(),
  listFills: vi.fn(),
  createAccount: vi.fn(),
  approveProposal: vi.fn(),
  rejectProposal: vi.fn(),
  listAccounts: vi.fn(),
  getRiskDecision: vi.fn(),
  updateMandate: vi.fn(),
  comparePerformance: vi.fn(),
  listRuns: vi.fn(),
  pause: vi.fn(),
  resume: vi.fn(),
  freeze: vi.fn(),
}));

vi.mock('../../api/market', () => ({
  marketApi: {
    getCandles,
    getAnnotations,
    streamUrl: (symbol: string) => `/api/v1/market/${symbol}/stream`,
  },
}));

vi.mock('../../api/paper', () => ({
  paperApi: {
    inspect,
    listOrders,
    listFills,
    createAccount,
    approveProposal,
    rejectProposal,
    listAccounts,
    getRiskDecision,
    updateMandate,
    comparePerformance,
    listRuns,
    pause,
    resume,
    freeze,
  },
}));

const account = {
  account: { id: 7, name: '策略账户', market: 'cn', baseCurrency: 'CNY', isActive: true },
  config: { configId: 'cfg-7', accountId: 7, configVersion: 2, controllerKind: 'llm', approvalMode: 'recommend_only', mandateVersion: '1', mandate: {}, initialCash: 100000, state: 'active', enabled: true },
  proposals: [{
    proposalId: 'proposal-1',
    accountId: 7,
    proposalVersion: '1',
    symbol: '600519',
    market: 'cn',
    side: 'buy',
    orderType: 'market',
    quantity: 100,
    targetWeight: null,
    limitPrice: null,
    stopPrice: null,
    rationale: '趋势与成交量同步改善',
    evidenceRefs: [],
    proposalHash: 'proposal-hash',
    status: 'pending_confirmation',
  }],
};

beforeEach(() => {
  vi.clearAllMocks();
  getCandles.mockResolvedValue({
    securityId: { symbol: '600519', market: 'cn' },
    period: 'daily',
    candles: [
      { timestamp: '2025-01-01', open: 10, high: 11, low: 9, close: 10, volume: 100, indicators: { macd: 0, rsi14: null } },
      { timestamp: '2025-01-02', open: 10, high: 12, low: 10, close: 11, volume: 120, changePercent: 10, indicators: { macd: 0.08, rsi14: null } },
    ],
    source: 'test',
    sourceStatus: 'ok',
    fallbackChain: [],
    asOf: '2025-01-02',
    stale: false,
    dataQuality: 'ok',
    limitations: [],
  });
  getAnnotations.mockResolvedValue({ symbol: '600519', items: [{ type: 'paper_proposal', source: 'paper', timestamp: '2025-01-02', symbol: '600519', payload: { proposalId: 'proposal-1' } }], page: 1, pageSize: 100, total: 1, hasMore: false, partial: false, limitations: [] });
  inspect.mockResolvedValue(account);
  listOrders.mockResolvedValue({ items: [] });
  listFills.mockResolvedValue({ items: [] });
  listAccounts.mockResolvedValue({ items: [{ account: account.account, config: account.config }] });
  listRuns.mockResolvedValue({ items: [{ runId: 'run-1', accountId: 7, decisionAt: '2025-01-02T15:00:00', strategyVersion: 'v1', status: 'completed', diagnostics: {} }], page: 1, pageSize: 100, total: 1, hasMore: false });
  getRiskDecision.mockRejectedValue(new Error('no risk decision in fixture'));
  updateMandate.mockResolvedValue(account);
  comparePerformance.mockResolvedValue({ startDate: '2025-01-01', endDate: '2025-12-31', items: [] });
  pause.mockResolvedValue({ ...account, config: { ...account.config, state: 'paused', configVersion: 3 } });
  resume.mockResolvedValue(account);
  freeze.mockResolvedValue({ ...account, config: { ...account.config, state: 'frozen', configVersion: 3 } });
  createAccount.mockResolvedValue({ ...account, account: { ...account.account, id: 8, name: '新账户' }, config: { ...account.config, accountId: 8 } });
  approveProposal.mockResolvedValue({ ...account.proposals[0], status: 'approved' });
  rejectProposal.mockResolvedValue({ ...account.proposals[0], status: 'rejected' });
});

describe('PaperWorkbenchPage', () => {
  it('loads the market preview and reads a Paper account', async () => {
    render(<PaperWorkbenchPage />);

    expect(await screen.findByText('市场与 Paper 交易工作台')).toBeInTheDocument();
    expect(await screen.findByText('最新收盘')).toBeInTheDocument();
    expect(screen.getByText('paper_proposal')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('账户 ID'), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: '读取账户' }));
    expect(await screen.findByText(/策略账户/)).toBeInTheDocument();
    expect(screen.getByText('趋势与成交量同步改善')).toBeInTheDocument();
    expect(inspect).toHaveBeenCalledWith(7);
  });

  it('approves a proposal and lets the server create its virtual order', async () => {
    render(<PaperWorkbenchPage />);
    fireEvent.change(screen.getByLabelText('账户 ID'), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: '读取账户' }));
    await screen.findAllByText(/策略账户/);

    fireEvent.click(screen.getByRole('button', { name: '批准' }));
    await waitFor(() => expect(approveProposal).toHaveBeenCalledWith(7, 'proposal-1'));
    expect(await screen.findByText('Proposal 已批准，虚拟订单已进入订单列表。')).toBeInTheDocument();
  });

  it('creates a local Paper account without exposing a broker action', async () => {
    render(<PaperWorkbenchPage />);
    fireEvent.click(screen.getByRole('button', { name: '创建' }));

    await waitFor(() => expect(createAccount).toHaveBeenCalledWith(expect.objectContaining({ name: '我的 Paper 账户', initialCash: 100000 })));
    expect(await screen.findByText('Paper 账户已创建：#8')).toBeInTheDocument();
    expect(screen.getAllByText(/新账户/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/真实下单|连接券商/)).not.toBeInTheDocument();
  });

  it('requires explicit confirmation before freezing a Paper account', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<PaperWorkbenchPage />);
    fireEvent.change(screen.getByLabelText('账户 ID'), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: '读取账户' }));
    await screen.findByText('PAPER ONLY · 无真实券商执行');

    fireEvent.click(screen.getByRole('button', { name: '冻结' }));
    expect(confirm).toHaveBeenCalled();
    expect(freeze).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it('surfaces optimistic concurrency conflicts without hiding the account', async () => {
    updateMandate.mockRejectedValueOnce({ response: { status: 409, data: { detail: { message: 'paper account version conflict' } } } });
    const paused = { ...account, config: { ...account.config, state: 'paused' } };
    inspect.mockResolvedValueOnce(paused);
    render(<PaperWorkbenchPage />);
    fireEvent.change(screen.getByLabelText('账户 ID'), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: '读取账户' }));
    await screen.findByText('PAPER ONLY · 无真实券商执行');
    fireEvent.click(screen.getByRole('button', { name: '保存 Mandate' }));
    expect(await screen.findByText(/version conflict/)).toBeInTheDocument();
    expect(screen.getAllByText(/策略账户/).length).toBeGreaterThan(0);
  });
});
