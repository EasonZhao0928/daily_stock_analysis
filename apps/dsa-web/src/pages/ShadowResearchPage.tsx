import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { FlaskConical, Play, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react';
import { shadowApi } from '../api/shadow';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import {
  AppPage,
  Badge,
  Button,
  Card,
  EmptyState,
  InlineAlert,
  Loading,
  PageHeader,
} from '../components/common';
import type {
  ShadowBacktestRequest,
  ShadowObservation,
  ShadowProfileDetail,
  ShadowSignal,
} from '../types/shadow';

const INPUT_CLASS =
  'input-surface input-focus-glow h-10 w-full rounded-xl border bg-transparent px-3 text-sm transition-all focus:outline-none';
const TEXTAREA_CLASS =
  'input-surface input-focus-glow min-h-32 w-full rounded-xl border bg-transparent px-3 py-2 font-mono text-xs leading-5 transition-all focus:outline-none';

const DEFAULT_RULE = JSON.stringify({
  all: [
    { feature: 'close', op: 'gt', value: 0 },
    { feature: 'volume_ratio', op: 'gte', value: 1 },
  ],
}, null, 2);

const DEFAULT_OBSERVATIONS = JSON.stringify([
  { date: '2025-01-02', features: { close: 10, volume_ratio: 1.2 }, next_return_pct: 1.5 },
  { date: '2025-01-03', features: { close: 11, volume_ratio: 0.8 }, next_return_pct: -0.4 },
  { date: '2025-01-06', features: { close: 12, volume_ratio: 1.4 }, next_return_pct: 2.1 },
], null, 2);

const STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  degraded: '已退化',
  approved: '已批准',
  disabled: '已停用',
  frozen: '已冻结',
};

function statusVariant(status: string): 'default' | 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'approved') return 'success';
  if (status === 'degraded') return 'warning';
  if (status === 'disabled') return 'danger';
  if (status === 'frozen') return 'info';
  return 'default';
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label} 必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function parseObservations(value: string): ShadowObservation[] {
  const parsed: unknown = JSON.parse(value);
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error('观测数据必须是非空 JSON 数组');
  }
  return parsed as ShadowObservation[];
}

function formatMetric(metrics: Record<string, unknown> | undefined, key: string): string {
  const value = metrics?.[key];
  if (value === null || value === undefined) return '--';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2);
  return String(value);
}

const ProfileCard: React.FC<{
  detail: ShadowProfileDetail;
  selected: boolean;
  onSelect: () => void;
}> = ({ detail, selected, onSelect }) => {
  const profile = detail.profile;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full rounded-2xl border p-4 text-left transition-colors ${selected ? 'border-cyan/50 bg-cyan/8' : 'border-border/60 bg-card/60 hover:bg-hover'}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-semibold text-foreground">{profile.name}</p>
          <p className="mt-1 truncate font-mono text-[11px] text-secondary-text">{profile.profileId}</p>
        </div>
        <Badge variant={statusVariant(profile.status)}>{STATUS_LABELS[profile.status] ?? profile.status}</Badge>
      </div>
      {profile.description ? <p className="mt-3 line-clamp-2 text-xs text-secondary-text">{profile.description}</p> : null}
      {detail.latestRun ? (
        <p className="mt-3 text-xs text-secondary-text">
          最近回测：{detail.latestRun.status} · OOS {formatMetric(detail.latestRun.metrics, 'out_sample_count')} 条
        </p>
      ) : (
        <p className="mt-3 text-xs text-secondary-text">尚未执行回测</p>
      )}
    </button>
  );
};

const ShadowResearchPage: React.FC = () => {
  const [profiles, setProfiles] = useState<ShadowProfileDetail[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null);
  const [signals, setSignals] = useState<ShadowSignal[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<'create' | 'backtest' | 'approve' | 'scan' | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [profileName, setProfileName] = useState('成交量突破研究');
  const [profileDescription, setProfileDescription] = useState('只生成可审计的研究信号，不连接真实账户。');
  const [ruleText, setRuleText] = useState(DEFAULT_RULE);
  const [code, setCode] = useState('600519');
  const [splitDate, setSplitDate] = useState('2025-01-03');
  const [observationText, setObservationText] = useState(DEFAULT_OBSERVATIONS);
  const selected = useMemo(
    () => profiles.find((item) => item.profile.profileId === selectedProfileId) ?? null,
    [profiles, selectedProfileId],
  );

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    try {
      const response = await shadowApi.listProfiles();
      setProfiles(response.items ?? []);
      setSelectedProfileId((current) => (
        current && response.items.some((item) => item.profile.profileId === current)
          ? current
          : response.items[0]?.profile.profileId ?? null
      ));
      setError(null);
    } catch (caught) {
      setError(getParsedApiError(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    document.title = 'Shadow Research - DSA';
    void loadProfiles();
  }, [loadProfiles]);

  useEffect(() => {
    if (!selectedProfileId) {
      setSignals([]);
      return;
    }
    let active = true;
    void shadowApi.listSignals(selectedProfileId, code)
      .then((response) => {
        if (active) setSignals(response.items ?? []);
      })
      .catch(() => {
        if (active) setSignals([]);
      });
    return () => {
      active = false;
    };
  }, [code, selectedProfileId]);

  const updateDetail = (detail: ShadowProfileDetail) => {
    setProfiles((current) => {
      const exists = current.some((item) => item.profile.profileId === detail.profile.profileId);
      return exists
        ? current.map((item) => (item.profile.profileId === detail.profile.profileId ? detail : item))
        : [detail, ...current];
    });
    setSelectedProfileId(detail.profile.profileId);
  };

  const handleCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy('create');
    setLocalError(null);
    setNotice(null);
    try {
      const detail = await shadowApi.createProfile({
        name: profileName,
        description: profileDescription,
        rule: parseJsonObject(ruleText, '规则'),
      });
      updateDetail(detail);
      setNotice('Shadow profile 已创建，执行样本外回测后才能批准。');
    } catch (caught) {
      if (caught instanceof SyntaxError || caught instanceof Error && !('response' in caught)) {
        setLocalError(caught instanceof Error ? caught.message : '规则 JSON 无效');
      } else {
        setError(getParsedApiError(caught));
      }
    } finally {
      setBusy(null);
    }
  };

  const handleBacktest = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!selected) return;
    setBusy('backtest');
    setLocalError(null);
    setNotice(null);
    try {
      const payload: ShadowBacktestRequest = {
        code,
        splitDate,
        observations: parseObservations(observationText),
      };
      const response = await shadowApi.runBacktest(selected.profile.profileId, payload);
      updateDetail({ ...selected, latestRun: response.run });
      setNotice(response.run.status === 'completed' ? '样本外回测完成，可以提交批准。' : '回测已降级：没有足够的样本外数据。');
    } catch (caught) {
      if (caught instanceof SyntaxError || caught instanceof Error && !('response' in caught)) {
        setLocalError(caught instanceof Error ? caught.message : '观测数据 JSON 无效');
      } else {
        setError(getParsedApiError(caught));
      }
    } finally {
      setBusy(null);
    }
  };

  const handleApprove = async () => {
    if (!selected) return;
    setBusy('approve');
    setError(null);
    setNotice(null);
    try {
      const detail = await shadowApi.approveProfile(selected.profile.profileId);
      updateDetail(detail);
      setNotice('Profile 已批准；现在只会生成影子研究信号，不会提交真实订单。');
    } catch (caught) {
      setError(getParsedApiError(caught));
    } finally {
      setBusy(null);
    }
  };

  const handleScan = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!selected) return;
    setBusy('scan');
    setLocalError(null);
    setNotice(null);
    try {
      const response = await shadowApi.scanSignals(selected.profile.profileId, {
        code,
        observations: parseObservations(observationText),
        runId: selected.latestRun?.runId,
      });
      setSignals(response.items ?? []);
      setNotice(`扫描完成：${response.items?.length ?? 0} 条可审计影子信号。`);
    } catch (caught) {
      if (caught instanceof SyntaxError || caught instanceof Error && !('response' in caught)) {
        setLocalError(caught instanceof Error ? caught.message : '观测数据 JSON 无效');
      } else {
        setError(getParsedApiError(caught));
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <AppPage>
      <PageHeader
        eyebrow="Shadow Research"
        title="影子研究账户"
        description="将受限规则在冻结的历史数据上回测，经过样本外审批后生成可复盘信号；本页面不会触达真实券商账户。"
        actions={(
          <Button variant="secondary" size="sm" onClick={() => void loadProfiles()} disabled={loading}>
            <RefreshCw className="h-4 w-4" /> 刷新
          </Button>
        )}
      />

      {error ? <ApiErrorInline error={error} onRetry={() => { setError(null); void loadProfiles(); }} /> : null}
      {localError ? <InlineAlert variant="danger" title="输入校验失败" message={localError} className="mt-4" /> : null}
      {notice ? <InlineAlert variant="success" message={notice} className="mt-4" /> : null}

      <div className="mt-5 grid gap-5 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <Card title="新建研究规则" subtitle="受限 DSL">
          <form className="space-y-4" onSubmit={handleCreate}>
            <label className="block text-sm text-secondary-text">
              名称
              <input className={`${INPUT_CLASS} mt-2`} value={profileName} onChange={(event) => setProfileName(event.target.value)} required />
            </label>
            <label className="block text-sm text-secondary-text">
              描述
              <input className={`${INPUT_CLASS} mt-2`} value={profileDescription} onChange={(event) => setProfileDescription(event.target.value)} />
            </label>
            <label className="block text-sm text-secondary-text">
              规则 JSON
              <textarea className={`${TEXTAREA_CLASS} mt-2`} value={ruleText} onChange={(event) => setRuleText(event.target.value)} aria-label="规则 JSON" />
            </label>
            <div className="flex items-center gap-2 text-xs text-secondary-text">
              <ShieldCheck className="h-4 w-4 text-cyan" /> 仅允许白名单 feature/operator，禁止 eval、Shell 和动态字段。
            </div>
            <Button type="submit" isLoading={busy === 'create'} loadingText="创建中...">
              <Sparkles className="h-4 w-4" /> 创建 profile
            </Button>
          </form>
        </Card>

        <Card title="研究 profiles" subtitle={`${profiles.length} 个 profile`}>
          {loading ? <Loading label="正在加载 Shadow profiles" /> : profiles.length === 0 ? (
            <EmptyState icon={<FlaskConical className="h-7 w-7" />} title="暂无 Shadow profile" description="先创建一条受限规则，再执行样本外回测。" />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {profiles.map((detail) => (
                <ProfileCard
                  key={detail.profile.profileId}
                  detail={detail}
                  selected={detail.profile.profileId === selectedProfileId}
                  onSelect={() => setSelectedProfileId(detail.profile.profileId)}
                />
              ))}
            </div>
          )}
        </Card>
      </div>

      {selected ? (
        <div className="mt-5 grid gap-5 xl:grid-cols-2">
          <Card title={`回测与审批 · ${selected.profile.name}`} subtitle="时间切分 / OOS">
            <form className="space-y-4" onSubmit={handleBacktest}>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-sm text-secondary-text">
                  股票代码
                  <input className={`${INPUT_CLASS} mt-2`} value={code} onChange={(event) => setCode(event.target.value)} required />
                </label>
                <label className="block text-sm text-secondary-text">
                  样本外起点
                  <input className={`${INPUT_CLASS} mt-2`} type="date" value={splitDate} onChange={(event) => setSplitDate(event.target.value)} required />
                </label>
              </div>
              <label className="block text-sm text-secondary-text">
                冻结观测 JSON
                <textarea className={`${TEXTAREA_CLASS} mt-2 min-h-52`} value={observationText} onChange={(event) => setObservationText(event.target.value)} aria-label="冻结观测 JSON" />
              </label>
              <div className="flex flex-wrap gap-2">
                <Button type="submit" isLoading={busy === 'backtest'} loadingText="回测中...">
                  <Play className="h-4 w-4" /> 执行回测
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => void handleApprove()}
                  isLoading={busy === 'approve'}
                  disabled={selected.latestRun?.status !== 'completed' || selected.profile.status === 'approved'}
                >
                  <ShieldCheck className="h-4 w-4" /> 批准 profile
                </Button>
              </div>
            </form>
            {selected.latestRun ? (
              <div className="mt-5 rounded-2xl border border-border/60 bg-elevated/40 p-4 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">最近一次回测</span>
                  <Badge variant={selected.latestRun.status === 'completed' ? 'success' : 'warning'}>{selected.latestRun.status}</Badge>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-3 text-xs text-secondary-text sm:grid-cols-4">
                  <Metric label="样本数" value={formatMetric(selected.latestRun.metrics, 'observation_count')} />
                  <Metric label="样本外信号" value={formatMetric(selected.latestRun.metrics, 'out_sample_signal_count')} />
                  <Metric label="平均收益" value={formatMetric(selected.latestRun.metrics, 'out_sample_avg_return_pct')} />
                  <Metric label="胜率" value={formatMetric(selected.latestRun.metrics, 'out_sample_win_rate_pct')} />
                </div>
                <p className="mt-3 break-all font-mono text-[11px] text-secondary-text">snapshot: {selected.latestRun.sourceSnapshotHash}</p>
              </div>
            ) : null}
          </Card>

          <Card title="扫描影子信号" subtitle="仅 approved profile">
            <form className="space-y-4" onSubmit={handleScan}>
              <p className="text-sm text-secondary-text">当前状态：<Badge variant={statusVariant(selected.profile.status)}>{STATUS_LABELS[selected.profile.status] ?? selected.profile.status}</Badge></p>
              <p className="text-xs text-secondary-text">扫描复用上面的冻结观测；数据 cutoff、rule version、source snapshot hash 会随 signal 保存。</p>
              <Button type="submit" variant="gradient" isLoading={busy === 'scan'} disabled={selected.profile.status !== 'approved'} loadingText="扫描中...">
                <FlaskConical className="h-4 w-4" /> 生成影子信号
              </Button>
            </form>
            <div className="mt-5 border-t border-border/60 pt-4">
              <h3 className="text-sm font-semibold">最近信号</h3>
              {signals.length === 0 ? (
                <p className="mt-3 text-sm text-secondary-text">暂无信号；未批准或规则未命中的状态不会写入可交易信号。</p>
              ) : (
                <div className="mt-3 space-y-2">
                  {signals.map((signal) => <SignalRow key={signal.signalId} signal={signal} />)}
                </div>
              )}
            </div>
          </Card>
        </div>
      ) : null}
    </AppPage>
  );
};

const Metric: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div>
    <p>{label}</p>
    <p className="mt-1 font-medium text-foreground">{value}</p>
  </div>
);

const SignalRow: React.FC<{ signal: ShadowSignal }> = ({ signal }) => (
  <div className="rounded-xl border border-border/60 bg-elevated/35 px-3 py-2 text-xs">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="font-mono text-foreground">{signal.code} · {signal.signalDate}</span>
      <Badge variant="info">{signal.status}</Badge>
    </div>
    <p className="mt-1 break-all text-secondary-text">cutoff {signal.dataCutoff} · {signal.reason ?? 'rule_matched'}</p>
  </div>
);

const ApiErrorInline: React.FC<{ error: ParsedApiError; onRetry: () => void }> = ({ error, onRetry }) => (
  <InlineAlert
    variant="danger"
    title={error.title}
    message={error.message}
    className="mt-4"
    action={<Button size="sm" variant="secondary" onClick={onRetry}>重试</Button>}
  />
);

export default ShadowResearchPage;
