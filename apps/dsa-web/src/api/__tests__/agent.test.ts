import { beforeEach, describe, expect, it, vi } from 'vitest';
import { agentApi } from '../agent';

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock('../index', () => ({
  default: {
    get,
    post,
    delete: vi.fn(),
  },
}));

describe('agentApi', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it('uses the shared camelCase Agent backend status contract', async () => {
    get.mockResolvedValueOnce({
      data: {
        backend: 'codex_app_server',
        available: false,
        experimental: true,
        version: '0.144.3',
        error_code: 'login_required',
        message: 'Codex login is required',
      },
    });

    const result = await agentApi.getStatus();

    expect(get).toHaveBeenCalledWith('/api/v1/agent/status');
    expect(result).toEqual({
      backend: 'codex_app_server',
      available: false,
      experimental: true,
      version: '0.144.3',
      errorCode: 'login_required',
      message: 'Codex login is required',
    });
  });

  it('returns session messages together with persisted Skill state', async () => {
    get.mockResolvedValueOnce({
      data: {
        session_id: 'session-1',
        messages: [
          { id: '1', role: 'user', content: '分析 AAPL', created_at: null },
        ],
        session_state: {
          selected_skill_ids: ['technical', 'risk'],
        },
      },
    });

    const result = await agentApi.getChatSessionMessages('session-1');

    expect(get).toHaveBeenCalledWith('/api/v1/agent/chat/sessions/session-1');
    expect(result.session_state.selected_skill_ids).toEqual(['technical', 'risk']);
  });

  it('preserves null when a legacy session has no persisted Skill state', async () => {
    get.mockResolvedValueOnce({
      data: {
        session_id: 'legacy-session',
        messages: [
          { id: '1', role: 'user', content: '继续分析', created_at: null },
        ],
        session_state: {
          selected_skill_ids: null,
        },
      },
    });

    const result = await agentApi.getChatSessionMessages('legacy-session');

    expect(result.session_state.selected_skill_ids).toBeNull();
  });

  it('reads the token-free Codex account status and nested rate-limit windows', async () => {
    get.mockResolvedValueOnce({
      data: {
        account: {
          status: 'authenticated',
          auth_method: 'chatgpt',
          email: 'investor@example.test',
          plan_type: 'plus',
          requires_openai_auth: false,
        },
        rate_limits: {
          snapshots: [{
            limit_id: 'codex',
            primary: {
              bucket: 'primary',
              used_percent: 12,
              window_duration_minutes: 300,
              resets_at: 1700000000,
            },
          }],
        },
        rate_limit_error_code: null,
        notifications: [],
      },
    });

    const result = await agentApi.getCodexAccountStatus();

    expect(get).toHaveBeenCalledWith('/api/v1/agent/account');
    expect(result.account.planType).toBe('plus');
    expect(result.rateLimits?.snapshots[0].primary?.resetsAt).toBe(1700000000);
  });

  it('uses opaque login IDs for browser login, cancellation, and logout actions', async () => {
    post
      .mockResolvedValueOnce({
        data: {
          mode: 'browser',
          status: 'pending',
          login_id: 'login-1',
          auth_url: 'https://auth.example.test',
        },
      })
      .mockResolvedValueOnce({ data: { status: 'cancelled' } })
      .mockResolvedValueOnce({ data: { status: 'signed_out' } });

    const login = await agentApi.startCodexLogin();
    const cancelled = await agentApi.cancelCodexLogin(login.loginId);
    const logout = await agentApi.logoutCodexAccount();

    expect(login.loginId).toBe('login-1');
    expect(cancelled.status).toBe('cancelled');
    expect(logout.status).toBe('signed_out');
    expect(post).toHaveBeenNthCalledWith(1, '/api/v1/agent/account/login', { mode: 'browser' });
    expect(post).toHaveBeenNthCalledWith(2, '/api/v1/agent/account/login/cancel', { login_id: 'login-1' });
    expect(post).toHaveBeenNthCalledWith(3, '/api/v1/agent/account/logout');
  });
});
