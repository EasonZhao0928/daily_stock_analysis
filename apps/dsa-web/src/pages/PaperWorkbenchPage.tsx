import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { Check, Pause, Play, RefreshCw, ShieldAlert, Snowflake, Wallet, X } from 'lucide-react';
import { marketApi } from '../api/market';
import { paperApi } from '../api/paper';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import { API_BASE_URL } from '../utils/constants';
import {
  AppPage,
  Badge,
  Button,
  Card,
  EmptyState,
  InlineAlert,
  PageHeader,
} from '../components/common';
import { CandlestickChart } from '../components/market/CandlestickChart';
import type { MarketAnnotation, MarketCandle, MarketCandleResponse } from '../types/market';
import type { PaperAccountInspect, PaperAccountListItem, PaperFill, PaperOrder, PaperPerformanceComparison, PaperProposal, PaperRiskDecision, PaperRun } from '../types/paper';

const INPUT_CLASS = 'input-surface input-focus-glow h-10 w-full rounded-xl border bg-transparent px-3 text-sm transition-all focus:outline-none';

function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(Number(value))) return '--';
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: digits });
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '--';
  return String(value).replace('T', ' ').slice(0, 19);
}

function statusVariant(status: string): 'default' | 'success' | 'warning' | 'danger' | 'info' {
  if (['approved', 'filled', 'active', 'completed'].includes(status)) return 'success';
  if (['pending', 'open', 'partially_filled', 'staged'].includes(status)) return 'info';
  if (['rejected', 'cancelled', 'closed'].includes(status)) return 'danger';
  if (['frozen', 'paused', 'expired', 'degraded'].includes(status)) return 'warning';
  return 'default';
}

const AnnotationList: React.FC<{ items: MarketAnnotation[] }> = ({ items }) => (
  <div className="space-y-2">
    {items.length === 0 ? (
      <EmptyState title="暂无事件标注" description="账户成交、Paper Fill、影子信号和告警会按时间叠加到这里。" />
    ) : items.map((item, index) => (
      <div key={`${item.type}-${item.timestamp}-${index}`} className="rounded-xl border border-border/50 bg-card/45 px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Badge variant={item.source === 'paper' ? 'info' : 'default'}>{item.type}</Badge>
            <span className="truncate text-xs text-secondary-text">{item.source}</span>
          </div>
          <span className="shrink-0 text-[11px] text-secondary-text">{formatDate(item.timestamp)}</span>
        </div>
        <p className="mt-1 truncate font-mono text-[11px] text-secondary-text">{JSON.stringify(item.payload)}</p>
      </div>
    ))}
  </div>
);

const ProposalRow: React.FC<{
  proposal: PaperProposal;
  risk?: PaperRiskDecision;
  busy: string | null;
  onApprove: () => void;
  onReject: () => void;
  approvalMode: string;
}> = ({ proposal, risk, busy, onApprove, onReject, approvalMode }) => {
  const pendingConfirmation = proposal.status === 'pending_confirmation';
  const rejectable = pendingConfirmation || proposal.status === 'recommended';
  return (
    <div className="rounded-2xl border border-border/60 bg-card/45 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-foreground">{proposal.symbol}</span>
            <Badge variant={proposal.side.toLowerCase() === 'buy' ? 'success' : 'warning'}>{proposal.side}</Badge>
            <Badge variant={statusVariant(proposal.status)}>{proposal.status}</Badge>
          </div>
          <p className="mt-1 text-xs text-secondary-text">{proposal.orderType} · 数量 {formatNumber(proposal.quantity, 4)} · 限价 {formatNumber(proposal.limitPrice)}</p>
          {risk ? <p className="mt-1 text-xs text-secondary-text">Risk: <span className={risk.decision === 'accepted' ? 'text-success' : 'text-warning'}>{risk.decision}</span>{risk.ruleCodes.length ? ` · ${risk.ruleCodes.join(', ')}` : ''}</p> : null}
        </div>
        <div className="flex flex-wrap gap-2">
          {pendingConfirmation ? <Button size="sm" variant="primary" onClick={onApprove} isLoading={busy === `approve:${proposal.proposalId}`}><Check className="h-4 w-4" />批准</Button> : null}
          {rejectable ? <Button size="sm" variant="danger-subtle" onClick={onReject} isLoading={busy === `reject:${proposal.proposalId}`}><X className="h-4 w-4" />拒绝</Button> : null}
        </div>
      </div>
      {proposal.rationale ? <p className="mt-3 text-sm leading-6 text-secondary-text">{proposal.rationale}</p> : null}
      <p className="mt-2 text-[11px] text-secondary-text">审批策略：{approvalMode}</p>
      <p className="mt-2 font-mono text-[11px] text-secondary-text">{proposal.proposalId} · {formatDate(proposal.createdAt)}</p>
    </div>
  );
};

const PaperWorkbenchPage: React.FC = () => {
  const [symbol, setSymbol] = useState(() => {
    if (typeof window === 'undefined') return '600519';
    return new URLSearchParams(window.location.search).get('symbol')?.trim() || '600519';
  });
  const [period, setPeriod] = useState('daily');
  const [candles, setCandles] = useState<MarketCandle[]>([]);
  const [candleMeta, setCandleMeta] = useState<MarketCandleResponse | null>(null);
  const [annotations, setAnnotations] = useState<MarketAnnotation[]>([]);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState<ParsedApiError | null>(null);
  const [accountIdText, setAccountIdText] = useState('');
  const [paperAccounts, setPaperAccounts] = useState<PaperAccountListItem[]>([]);
  const [account, setAccount] = useState<PaperAccountInspect | null>(null);
  const [orders, setOrders] = useState<PaperOrder[]>([]);
  const [fills, setFills] = useState<PaperFill[]>([]);
  const [runs, setRuns] = useState<PaperRun[]>([]);
  const [accountLoading, setAccountLoading] = useState(false);
  const [accountError, setAccountError] = useState<ParsedApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [createName, setCreateName] = useState('我的 Paper 账户');
  const [createCash, setCreateCash] = useState('100000');
  const [mandateText, setMandateText] = useState('{}');
  const [mandateVersion, setMandateVersion] = useState('1');
  const [riskByProposal, setRiskByProposal] = useState<Record<string, PaperRiskDecision>>({});
  const [performanceStart, setPerformanceStart] = useState('2025-01-01');
  const [performanceEnd, setPerformanceEnd] = useState('2025-12-31');
  const [performance, setPerformance] = useState<PaperPerformanceComparison | null>(null);
  const [streamDegraded, setStreamDegraded] = useState(false);

  const loadMarket = useCallback(async () => {
    const normalized = symbol.trim();
    if (!normalized) return;
    setMarketLoading(true);
    setMarketError(null);
    try {
      const [candleResponse, annotationResponse] = await Promise.all([
        marketApi.getCandles(normalized, { period, limit: 240 }),
        marketApi.getAnnotations(normalized),
      ]);
      setCandleMeta(candleResponse);
      setCandles(candleResponse.candles ?? []);
      setAnnotations(annotationResponse.items ?? []);
    } catch (caught) {
      setMarketError(getParsedApiError(caught));
    } finally {
      setMarketLoading(false);
    }
  }, [period, symbol]);

  const loadAccount = useCallback(async (value = accountIdText) => {
    const accountId = Number(value);
    if (!Number.isInteger(accountId) || accountId < 1) {
      setAccount(null);
      setOrders([]);
      setFills([]);
      return;
    }
    setAccountLoading(true);
    setAccountError(null);
    try {
      const [inspect, orderResponse, fillResponse, runResponse, annotationResponse] = await Promise.all([
        paperApi.inspect(accountId),
        paperApi.listOrders(accountId),
        paperApi.listFills(accountId),
        paperApi.listRuns(accountId),
        marketApi.getAnnotations(symbol.trim(), { accountId }),
      ]);
      const nextProposals = inspect.proposals ?? [];
      setAccount({ ...inspect, proposals: nextProposals });
      setOrders(orderResponse.items ?? []);
      setFills(fillResponse.items ?? []);
      setRuns(runResponse.items ?? []);
      setAnnotations(annotationResponse.items ?? []);
      setMandateText(JSON.stringify(inspect.config.mandate ?? {}, null, 2));
      setMandateVersion(inspect.config.mandateVersion || '1');
      const risks = await Promise.all(nextProposals.map(async (proposal) => {
        try {
          return [proposal.proposalId, await paperApi.getRiskDecision(accountId, proposal.proposalId)] as const;
        } catch {
          return null;
        }
      }));
      setRiskByProposal(Object.fromEntries(risks.filter((entry): entry is readonly [string, PaperRiskDecision] => entry !== null)));
    } catch (caught) {
      setAccountError(getParsedApiError(caught));
    } finally {
      setAccountLoading(false);
    }
  }, [accountIdText, symbol]);

  useEffect(() => {
    document.title = 'Paper Workbench - DSA';
    void loadMarket();
  }, [loadMarket]);

  useEffect(() => {
    void paperApi.listAccounts().then((response) => setPaperAccounts(response.items ?? [])).catch(() => setPaperAccounts([]));
  }, []);

  useEffect(() => {
    const eventSourceConstructor = (window as Window & { EventSource?: typeof EventSource }).EventSource;
    if (!eventSourceConstructor || !symbol.trim()) return undefined;
    const stream = new eventSourceConstructor(`${API_BASE_URL}${marketApi.streamUrl(symbol, { period, intervalSeconds: 10 })}`);
    const onSnapshot = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data) as MarketCandleResponse;
        if (Array.isArray(payload.candles)) {
          const normalized = { ...payload, candles: payload.candles };
          setCandleMeta(normalized);
          setCandles(payload.candles);
        }
      } catch {
        // A malformed stream event must not tear down the workbench.
      }
    };
    stream.addEventListener('snapshot', onSnapshot as EventListener);
    stream.addEventListener('stale', onSnapshot as EventListener);
    stream.addEventListener('error', () => setStreamDegraded(true));
    setStreamDegraded(false);
    return () => stream.close();
  }, [period, symbol]);

  const proposals = account?.proposals ?? [];
  const accountState = account?.config.state ?? 'unknown';

  const runAccountAction = async (action: 'pause' | 'resume' | 'freeze') => {
    if (!account) return;
    if (action === 'freeze' && !window.confirm('冻结后将阻止新的 Proposal/订单推进，并取消活动虚拟订单。确认冻结？')) return;
    setBusy(action);
    setAccountError(null);
    setNotice(null);
    try {
      const updated = await paperApi[action](account.account.id, account.config.configVersion);
      setAccount({ ...updated, proposals: updated.proposals ?? [] });
      setNotice(`Paper 账户已${action === 'pause' ? '暂停' : action === 'resume' ? '恢复' : '冻结'}。`);
      if (action === 'freeze') await loadAccount(String(account.account.id));
    } catch (caught) {
      setAccountError(getParsedApiError(caught));
    } finally {
      setBusy(null);
    }
  };

  const refreshAccount = () => void loadAccount();

  const handleMandateSave = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!account) return;
    setBusy('mandate');
    setAccountError(null);
    setNotice(null);
    try {
      const mandate = JSON.parse(mandateText) as Record<string, unknown>;
      if (!mandate || Array.isArray(mandate) || typeof mandate !== 'object') throw new Error('Mandate 必须是 JSON 对象');
      const updated = await paperApi.updateMandate(account.account.id, {
        mandate,
        mandateVersion,
        expectedVersion: account.config.configVersion,
      });
      setAccount({ ...updated, proposals: updated.proposals ?? [] });
      setNotice('Mandate 已保存；账户必须保持暂停或冻结状态。');
    } catch (caught) {
      if (caught instanceof SyntaxError || caught instanceof Error && !('response' in caught)) {
        setAccountError({ message: caught instanceof Error ? caught.message : 'Mandate JSON 无效' } as ParsedApiError);
      } else {
        setAccountError(getParsedApiError(caught));
      }
    } finally {
      setBusy(null);
    }
  };

  const handlePerformanceCompare = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!account) return;
    setBusy('performance');
    setAccountError(null);
    try {
      const result = await paperApi.comparePerformance([account.account.id], performanceStart, performanceEnd);
      setPerformance(result);
    } catch (caught) {
      setAccountError(getParsedApiError(caught));
    } finally {
      setBusy(null);
    }
  };

  const handleCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy('create');
    setAccountError(null);
    setNotice(null);
    try {
      const created = await paperApi.createAccount({ name: createName, initialCash: Number(createCash) || 0 });
      setAccountIdText(String(created.account.id));
      setAccount({ ...created, proposals: created.proposals ?? [] });
      setPaperAccounts((current) => [{ account: created.account, config: created.config }, ...current.filter((item) => item.account.id !== created.account.id)]);
      setOrders([]);
      setFills([]);
      setNotice(`Paper 账户已创建：#${created.account.id}`);
    } catch (caught) {
      setAccountError(getParsedApiError(caught));
    } finally {
      setBusy(null);
    }
  };

  const handleProposal = async (proposal: PaperProposal, action: 'approve' | 'reject') => {
    if (!account) return;
    const key = `${action}:${proposal.proposalId}`;
    setBusy(key);
    setAccountError(null);
    setNotice(null);
    try {
      if (action === 'approve') {
        await paperApi.approveProposal(account.account.id, proposal.proposalId);
      } else if (action === 'reject') {
        await paperApi.rejectProposal(account.account.id, proposal.proposalId);
      }
      await loadAccount(String(account.account.id));
      setNotice(action === 'approve' ? 'Proposal 已批准，虚拟订单已进入订单列表。' : 'Proposal 已拒绝。');
    } catch (caught) {
      setAccountError(getParsedApiError(caught));
    } finally {
      setBusy(null);
    }
  };

  return (
    <AppPage className="space-y-5">
      <PageHeader
        eyebrow="MARKET · PAPER"
        title="市场与 Paper 交易工作台"
        description="在同一份行情快照上查看 K 线、研究/交易标注，并人工审核大模型提出的虚拟账户 Proposal。所有动作仍停留在 Paper Account。"
        actions={<Button variant="secondary" onClick={() => { void loadMarket(); refreshAccount(); }} isLoading={marketLoading || accountLoading}><RefreshCw className="h-4 w-4" />刷新</Button>}
      />

      {marketError ? <InlineAlert title="行情加载失败" message={marketError.message} variant="danger" /> : null}
      {accountError ? <InlineAlert title="Paper 账户操作失败" message={accountError.message} variant="danger" /> : null}
      {notice ? <InlineAlert title="操作完成" message={notice} variant="success" /> : null}

      <Card title="行情与事件" subtitle="MARKET SNAPSHOT" className="space-y-4">
        <form className="grid gap-3 md:grid-cols-[minmax(0,1fr)_9rem_auto]" onSubmit={(event) => { event.preventDefault(); void loadMarket(); }}>
          <label className="space-y-1.5 text-xs text-secondary-text">
            股票代码
            <input className={INPUT_CLASS} value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="600519 / AAPL" aria-label="股票代码" />
          </label>
          <label className="space-y-1.5 text-xs text-secondary-text">
            周期
            <select className={`${INPUT_CLASS} appearance-none`} value={period} onChange={(event) => setPeriod(event.target.value)} aria-label="周期">
              <option value="daily">日线</option>
              <option value="weekly">周线</option>
              <option value="monthly">月线</option>
            </select>
          </label>
          <Button type="submit" className="self-end" isLoading={marketLoading}><RefreshCw className="h-4 w-4" />读取行情</Button>
        </form>
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(18rem,0.8fr)]">
          <div>
            <CandlestickChart candles={candles} annotations={annotations} stale={candleMeta?.stale} metadata={candleMeta ?? undefined} />
            {streamDegraded ? <p className="mt-1 text-xs text-warning">实时流已断开，保留最后一次快照；可手动刷新重试。</p> : null}
          </div>
          <div>
            <div className="mb-3 flex items-center justify-between"><h3 className="font-semibold text-foreground">事件标注</h3><Badge variant="info">{annotations.length} 条</Badge></div>
            <AnnotationList items={annotations} />
          </div>
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
        <Card title="Paper 账户" subtitle="VIRTUAL ACCOUNT" className="space-y-4">
          {paperAccounts.length ? (
            <label className="space-y-1.5 text-xs text-secondary-text">账户列表
              <select className={`${INPUT_CLASS} appearance-none`} value={accountIdText} onChange={(event) => { setAccountIdText(event.target.value); void loadAccount(event.target.value); }} aria-label="Paper 账户列表">
                <option value="">请选择账户</option>
                {paperAccounts.map((item) => <option key={item.account.id} value={item.account.id}>#{item.account.id} · {item.account.name} · {item.config.state}</option>)}
              </select>
            </label>
          ) : null}
          <div className="flex flex-wrap items-end gap-2">
            <label className="min-w-0 flex-1 space-y-1.5 text-xs text-secondary-text">账户 ID<input className={INPUT_CLASS} value={accountIdText} onChange={(event) => setAccountIdText(event.target.value)} placeholder="输入已存在的 account id" aria-label="账户 ID" /></label>
            <Button variant="outline" onClick={refreshAccount} isLoading={accountLoading}>读取账户</Button>
          </div>
          {account ? (
            <div className="rounded-2xl border border-border/60 bg-card/45 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div><p className="font-semibold text-foreground">#{account.account.id} · {account.account.name}</p><p className="mt-1 text-xs text-secondary-text">{account.account.market} · {account.account.baseCurrency} · {account.config.controllerKind}</p><Badge variant="info">PAPER ONLY · 无真实券商执行</Badge></div>
                <Badge variant={statusVariant(accountState)}>{accountState}</Badge>
              </div>
              <p className="mt-3 text-xs text-secondary-text">配置版本 {account.config.configVersion} · 审批模式 {account.config.approvalMode} · 初始现金 {formatNumber(account.config.initialCash)}</p>
              <div className="mt-4 flex flex-wrap gap-2">
                {accountState === 'active' ? <Button size="sm" variant="secondary" onClick={() => void runAccountAction('pause')} isLoading={busy === 'pause'}><Pause className="h-4 w-4" />暂停</Button> : null}
                {accountState === 'paused' ? <Button size="sm" variant="secondary" onClick={() => void runAccountAction('resume')} isLoading={busy === 'resume'}><Play className="h-4 w-4" />恢复</Button> : null}
                {accountState !== 'frozen' && accountState !== 'closed' ? <Button size="sm" variant="danger-subtle" onClick={() => void runAccountAction('freeze')} isLoading={busy === 'freeze'}><Snowflake className="h-4 w-4" />冻结</Button> : null}
              </div>
              <form className="mt-4 space-y-2 border-t border-border/50 pt-4" onSubmit={handleMandateSave}>
                <div className="flex items-center justify-between gap-2"><p className="text-sm font-semibold text-foreground">Mandate 编辑</p><span className="text-[11px] text-secondary-text">仅暂停/冻结可修改</span></div>
                <div className="flex gap-2"><input className={`${INPUT_CLASS} max-w-24`} value={mandateVersion} onChange={(event) => setMandateVersion(event.target.value)} aria-label="Mandate 版本" /><Button size="sm" type="submit" variant="outline" isLoading={busy === 'mandate'} disabled={accountState !== 'paused' && accountState !== 'frozen'}>保存 Mandate</Button></div>
                <textarea className="input-surface input-focus-glow min-h-24 w-full rounded-xl border bg-transparent px-3 py-2 font-mono text-xs leading-5 focus:outline-none" value={mandateText} onChange={(event) => setMandateText(event.target.value)} aria-label="Mandate JSON" />
              </form>
            </div>
          ) : (
            <EmptyState title="未选择 Paper 账户" description="输入 account id 读取，或在下方创建一个只用于虚拟交易的账户。" icon={<Wallet className="h-6 w-6" />} />
          )}
          <form className="space-y-3 border-t border-border/50 pt-4" onSubmit={handleCreate}>
            <p className="text-sm font-semibold text-foreground">创建本地 Paper 账户</p>
            <input className={INPUT_CLASS} value={createName} onChange={(event) => setCreateName(event.target.value)} aria-label="Paper 账户名称" />
            <div className="flex gap-2"><input className={INPUT_CLASS} type="number" min="0" value={createCash} onChange={(event) => setCreateCash(event.target.value)} aria-label="初始现金" /><Button type="submit" isLoading={busy === 'create'}>创建</Button></div>
          </form>
        </Card>

        <Card title="Proposal 审核" subtitle="HUMAN APPROVAL" className="space-y-3">
          {!account ? <EmptyState title="先读取 Paper 账户" description="大模型决策只会进入当前 Paper 账户，不会连接真实券商。" icon={<ShieldAlert className="h-6 w-6" />} /> : proposals.length === 0 ? <EmptyState title="暂无 Proposal" description="运行 Paper 决策周期后，待审核的结构化建议会出现在这里。" /> : proposals.map((proposal) => <ProposalRow key={proposal.proposalId} proposal={proposal} risk={riskByProposal[proposal.proposalId]} busy={busy} approvalMode={account.config.approvalMode} onApprove={() => void handleProposal(proposal, 'approve')} onReject={() => void handleProposal(proposal, 'reject')} />)}
        </Card>
      </div>

      {account ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card title="虚拟订单" subtitle="PAPER ORDERS">
            {orders.length === 0 ? <EmptyState title="暂无虚拟订单" description="批准数量明确的 Proposal 后，订单会自动进入列表。" /> : <div className="space-y-2">{orders.map((order) => <div key={order.orderId} className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border/50 bg-card/45 px-3 py-2.5 text-sm"><div><span className="font-semibold text-foreground">{order.symbol}</span><span className="ml-2 text-xs text-secondary-text">{order.side} · {formatNumber(order.quantity, 4)} · {order.executionPolicy}</span></div><div className="flex items-center gap-2"><Badge variant={statusVariant(order.status)}>{order.status}</Badge><span className="text-xs text-secondary-text">成交 {formatNumber(order.filledQuantity, 4)}</span></div></div>)}</div>}
          </Card>
          <Card title="成交与 Ledger Outbox" subtitle="PAPER FILLS">
            {fills.length === 0 ? <EmptyState title="暂无成交" description="订单匹配行情后，fill 和可投影 Ledger outbox 会显示在这里。" /> : <div className="space-y-2">{fills.map((fill) => <div key={fill.fillId} className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border/50 bg-card/45 px-3 py-2.5 text-sm"><div><span className="font-semibold text-foreground">{formatDate(fill.fillDate)}</span><span className="ml-2 text-xs text-secondary-text">数量 {formatNumber(fill.quantity, 4)} · 价格 {formatNumber(fill.price)}</span></div><div className="text-right"><Badge variant={statusVariant(fill.status)}>{fill.status}</Badge><p className="mt-1 text-[11px] text-secondary-text">{fill.ledgerOutboxId ? `outbox ${fill.ledgerOutboxId}` : '尚未投影'}</p></div></div>)}</div>}
          </Card>
          <Card title="绩效比较" subtitle="PERFORMANCE">
            <form className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]" onSubmit={handlePerformanceCompare}>
              <input className={INPUT_CLASS} type="date" value={performanceStart} onChange={(event) => setPerformanceStart(event.target.value)} aria-label="绩效开始日期" />
              <input className={INPUT_CLASS} type="date" value={performanceEnd} onChange={(event) => setPerformanceEnd(event.target.value)} aria-label="绩效结束日期" />
              <Button type="submit" variant="outline" isLoading={busy === 'performance'}>比较</Button>
            </form>
            {performance ? <div className="mt-3 space-y-1 text-xs text-secondary-text"><p>{performance.startDate} → {performance.endDate}</p><p>账户结果：{performance.items.length} 个</p>{performance.limitations?.length ? <p className="text-warning">{performance.limitations.join('；')}</p> : null}</div> : <p className="mt-3 text-xs text-secondary-text">运行后展示 Paper 账户与基准的收益、回撤和交易统计。</p>}
          </Card>
          <Card title="决策运行" subtitle="PAPER RUNS">
            {runs.length === 0 ? <EmptyState title="暂无决策运行" description="每次 Observation → Proposal → Risk cycle 会在这里留下可重放记录。" /> : <div className="space-y-2">{runs.slice(0, 20).map((run) => <div key={run.runId} className="rounded-xl border border-border/50 bg-card/45 px-3 py-2.5 text-xs"><div className="flex items-center justify-between gap-2"><span className="font-mono text-foreground">{run.strategyVersion}</span><Badge variant={statusVariant(run.status)}>{run.status}</Badge></div><p className="mt-1 text-secondary-text">{formatDate(run.decisionAt)} · {run.backend ?? 'deterministic'} / {run.model ?? '--'}</p></div>)}</div>}
          </Card>
        </div>
      ) : null}
    </AppPage>
  );
};

export default PaperWorkbenchPage;
