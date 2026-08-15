import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ShadowResearchPage from '../ShadowResearchPage';

const {
  listProfiles,
  createProfile,
  runBacktest,
  approveProfile,
  scanSignals,
  listSignals,
} = vi.hoisted(() => ({
  listProfiles: vi.fn(),
  createProfile: vi.fn(),
  runBacktest: vi.fn(),
  approveProfile: vi.fn(),
  scanSignals: vi.fn(),
  listSignals: vi.fn(),
}));

vi.mock('../../api/shadow', () => ({
  shadowApi: {
    listProfiles,
    createProfile,
    runBacktest,
    approveProfile,
    scanSignals,
    listSignals,
  },
}));

const profile = {
  profileId: 'shadow_breakout',
  name: '成交量突破研究',
  description: '只生成研究信号',
  status: 'draft',
  ruleVersion: '1',
};

const rule = {
  ruleId: 1,
  profileId: profile.profileId,
  version: '1',
  dsl: { feature: 'close', op: 'gt', value: 0 },
  ruleHash: 'rule-hash',
  featureNames: ['close'],
  status: 'draft',
};

const completedRun = {
  runId: 'shadow-run-1',
  profileId: profile.profileId,
  ruleId: 1,
  code: '600519',
  splitDate: '2025-01-03',
  sourceSnapshotHash: 'snapshot-hash',
  status: 'completed',
  metrics: {
    observationCount: 3,
    outSampleCount: 2,
    outSampleSignalCount: 1,
    outSampleAvgReturnPct: 1.2,
    outSampleWinRatePct: 100,
  },
};

function detail(status = 'draft', latestRun: typeof completedRun | null = null) {
  return { profile: { ...profile, status }, rule, latestRun };
}

beforeEach(() => {
  vi.clearAllMocks();
  listProfiles.mockResolvedValue({ items: [detail()] });
  listSignals.mockResolvedValue({ items: [] });
  createProfile.mockResolvedValue(detail());
  runBacktest.mockResolvedValue({ run: completedRun, metrics: completedRun.metrics });
  approveProfile.mockResolvedValue(detail('approved', completedRun));
  scanSignals.mockResolvedValue({
    items: [{
      signalId: 'shadow-signal-1',
      profileId: profile.profileId,
      ruleId: 1,
      runId: completedRun.runId,
      code: '600519',
      signalDate: '2025-01-06',
      status: 'eligible',
      featureSnapshotHash: 'signal-snapshot',
      dataCutoff: '2025-01-06',
      evidenceRefs: [],
      reason: 'rule_matched',
    }],
  });
});

describe('ShadowResearchPage', () => {
  it('renders profiles, frozen backtest metrics, and approval workflow', async () => {
    render(<ShadowResearchPage />);

    expect(await screen.findByText('成交量突破研究')).toBeInTheDocument();
    expect(screen.getByText('尚未执行回测')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '执行回测' }));

    await waitFor(() => expect(runBacktest).toHaveBeenCalledWith(
      profile.profileId,
      expect.objectContaining({ code: '600519', splitDate: '2025-01-03' }),
    ));
    expect(await screen.findByText('最近一次回测')).toBeInTheDocument();
    expect(screen.getByText('snapshot: snapshot-hash')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /批准 profile/ }));
    await waitFor(() => expect(approveProfile).toHaveBeenCalledWith(profile.profileId));
    expect(await screen.findByText(/Profile 已批准/)).toBeInTheDocument();
  });

  it('validates rule JSON locally and does not create an invalid profile', async () => {
    render(<ShadowResearchPage />);

    const ruleInput = screen.getByLabelText('规则 JSON');
    fireEvent.change(ruleInput, { target: { value: '{invalid' } });
    fireEvent.click(screen.getByRole('button', { name: /创建 profile/ }));

    expect(await screen.findByText('输入校验失败')).toBeInTheDocument();
    expect(createProfile).not.toHaveBeenCalled();
  });

  it('enables scanning only after approval and renders idempotent signals', async () => {
    render(<ShadowResearchPage />);
    await screen.findByText('成交量突破研究');
    fireEvent.click(screen.getByRole('button', { name: '执行回测' }));
    await screen.findByText('最近一次回测');
    fireEvent.click(screen.getByRole('button', { name: /批准 profile/ }));
    await screen.findByText(/Profile 已批准/);

    const scanButton = screen.getByRole('button', { name: /生成影子信号/ });
    expect(scanButton).toBeEnabled();
    fireEvent.click(scanButton);

    await waitFor(() => expect(scanSignals).toHaveBeenCalledWith(
      profile.profileId,
      expect.objectContaining({ code: '600519', runId: completedRun.runId }),
    ));
    expect(await screen.findByText('600519 · 2025-01-06')).toBeInTheDocument();
    expect(screen.getByText('cutoff 2025-01-06 · rule_matched')).toBeInTheDocument();
  });
});
