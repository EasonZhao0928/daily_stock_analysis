import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CheckCircle2, CircleAlert, CircleDashed, RefreshCw } from 'lucide-react';
import {
  agentApi,
  type CodexAccountLoginResponse,
  type CodexAccountStatusResponse,
  type CodexLoginMode,
} from '../../api/agent';
import { systemConfigApi } from '../../api/systemConfig';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type {
  AgentBackendStatusResponse,
  SystemConfigUpdateItem,
} from '../../types/systemConfig';
import { ApiErrorAlert, Badge, Button } from '../common';
import { SettingsAlert } from './SettingsAlert';

interface AgentBackendStatusPanelProps {
  items: SystemConfigUpdateItem[];
  maskToken: string;
  selectedBackend: string;
  agentArch: string;
  disabled?: boolean;
  onUseSingleAgent: () => void;
  onEnableAgentMode: () => void;
}

function backendLabel(backendId: string, t: ReturnType<typeof useUiLanguage>['t']): string {
  return backendId === 'codex_app_server'
    ? t('settings.agentBackendCodexLabel')
    : t('settings.agentBackendDefaultLabel');
}

function statusMessage(status: AgentBackendStatusResponse, t: ReturnType<typeof useUiLanguage>['t']): string {
  if (status.available) return t('settings.agentBackendCanTryDescription');
  if (status.errorCode === 'command_not_found') return t('settings.agentBackendCommandNotFound');
  if (status.errorCode === 'unsupported_agent_arch') return t('settings.agentBackendSingleOnly');
  if (status.errorCode === 'agent_mode_disabled') return t('settings.agentBackendModeDisabled');
  if (status.errorCode === 'platform_unsupported') return t('settings.agentBackendPlatformUnsupported');
  if (status.errorCode === 'invalid_timeout') return t('settings.agentBackendInvalidTimeout');
  return t('settings.agentBackendUnavailableDescription');
}

function StatusIcon({ status }: { status: AgentBackendStatusResponse }) {
  if (status.available) {
    return <CheckCircle2 className="h-4 w-4 text-success" aria-hidden="true" />;
  }
  if (status.errorCode) {
    return <CircleAlert className="h-4 w-4 text-warning" aria-hidden="true" />;
  }
  return <CircleDashed className="h-4 w-4 text-muted-text" aria-hidden="true" />;
}

function formatResetTime(timestamp: number | null): string {
  if (timestamp === null) return '—';
  const date = new Date(timestamp * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

export function AgentBackendStatusPanel({
  items,
  maskToken,
  selectedBackend,
  agentArch,
  disabled = false,
  onUseSingleAgent,
  onEnableAgentMode,
}: AgentBackendStatusPanelProps) {
  const { t } = useUiLanguage();
  const [statusResponse, setStatusResponse] = useState<AgentBackendStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [accountResponse, setAccountResponse] = useState<CodexAccountStatusResponse | null>(null);
  const [accountLoading, setAccountLoading] = useState(false);
  const [accountError, setAccountError] = useState<ParsedApiError | null>(null);
  const [accountActionLoading, setAccountActionLoading] = useState(false);
  const [pendingLogin, setPendingLogin] = useState<CodexAccountLoginResponse | null>(null);
  const refreshRequestIdRef = useRef(0);
  const accountRequestIdRef = useRef(0);
  const requestItems = useMemo(
    () => items.map((item) => ({ key: item.key, value: item.value })),
    [items],
  );
  const hasDraft = requestItems.length > 0;
  const isCodex = selectedBackend === 'codex_app_server';
  const hasArchitectureConflict = isCodex && agentArch !== 'single';
  const canManageAccount = isCodex && !hasDraft && !hasArchitectureConflict && statusResponse?.available === true;

  const refresh = useCallback(async () => {
    const requestId = refreshRequestIdRef.current + 1;
    refreshRequestIdRef.current = requestId;
    setError(null);
    if (hasArchitectureConflict) {
      setStatusResponse(null);
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    try {
      const next = hasDraft
        ? await systemConfigApi.previewAgentBackendStatus({ items: requestItems, maskToken })
        : await systemConfigApi.getAgentBackendStatus();
      if (refreshRequestIdRef.current === requestId) {
        setStatusResponse(next);
      }
    } catch (nextError: unknown) {
      if (refreshRequestIdRef.current === requestId) {
        setStatusResponse(null);
        setError(getParsedApiError(nextError));
      }
    } finally {
      if (refreshRequestIdRef.current === requestId) {
        setIsLoading(false);
      }
    }
  }, [hasArchitectureConflict, hasDraft, maskToken, requestItems]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const refreshAccount = useCallback(async () => {
    const requestId = accountRequestIdRef.current + 1;
    accountRequestIdRef.current = requestId;
    if (!canManageAccount) {
      setAccountResponse(null);
      setPendingLogin(null);
      setAccountError(null);
      setAccountLoading(false);
      return;
    }
    setAccountLoading(true);
    setAccountError(null);
    try {
      const next = await agentApi.getCodexAccountStatus();
      if (accountRequestIdRef.current === requestId) {
        setAccountResponse(next);
      }
    } catch (nextError: unknown) {
      if (accountRequestIdRef.current === requestId) {
        setAccountResponse(null);
        setAccountError(getParsedApiError(nextError));
      }
    } finally {
      if (accountRequestIdRef.current === requestId) {
        setAccountLoading(false);
      }
    }
  }, [canManageAccount]);

  useEffect(() => {
    void refreshAccount();
  }, [refreshAccount]);

  const startLogin = useCallback(async (mode: CodexLoginMode) => {
    setAccountActionLoading(true);
    setAccountError(null);
    try {
      const next = await agentApi.startCodexLogin(mode);
      setPendingLogin(next);
    } catch (nextError: unknown) {
      setAccountError(getParsedApiError(nextError));
    } finally {
      setAccountActionLoading(false);
    }
  }, []);

  const cancelLogin = useCallback(async () => {
    if (!pendingLogin) return;
    setAccountActionLoading(true);
    try {
      await agentApi.cancelCodexLogin(pendingLogin.loginId);
      setPendingLogin(null);
      await refreshAccount();
    } catch (nextError: unknown) {
      setAccountError(getParsedApiError(nextError));
    } finally {
      setAccountActionLoading(false);
    }
  }, [pendingLogin, refreshAccount]);

  const logout = useCallback(async () => {
    setAccountActionLoading(true);
    setAccountError(null);
    try {
      await agentApi.logoutCodexAccount();
      setPendingLogin(null);
      await refreshAccount();
    } catch (nextError: unknown) {
      setAccountError(getParsedApiError(nextError));
    } finally {
      setAccountActionLoading(false);
    }
  }, [refreshAccount]);

  const status = statusResponse;

  return (
    <div data-testid="agent-backend-status-panel" className="space-y-3 rounded-xl border settings-border bg-card/70 p-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <p className="text-sm font-semibold text-foreground">{t('settings.agentBackendStatus')}</p>
          <p className="mt-1 text-xs leading-5 text-muted-text">
            {t('settings.agentBackendStatusDescription')}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="settings-secondary"
            size="sm"
            disabled={disabled || isLoading || hasArchitectureConflict}
            isLoading={isLoading}
            loadingText={t('settings.agentBackendRefreshing')}
            onClick={() => void refresh()}
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            {t('settings.agentBackendRefresh')}
          </Button>
        </div>
      </div>

      {hasArchitectureConflict ? (
        <SettingsAlert
          title={t('settings.agentBackendSingleOnlyTitle')}
          message={t('settings.agentBackendSingleOnly')}
          variant="warning"
          actionLabel={t('settings.agentBackendUseSingle')}
          onAction={disabled ? undefined : onUseSingleAgent}
        />
      ) : null}
      {statusResponse?.errorCode === 'agent_mode_disabled' ? (
        <SettingsAlert
          title={t('settings.agentBackendModeDisabledTitle')}
          message={t('settings.agentBackendModeDisabled')}
          variant="warning"
          actionLabel={t('settings.agentBackendEnableMode')}
          onAction={disabled ? undefined : onEnableAgentMode}
        />
      ) : null}
      {isCodex ? (
        <SettingsAlert
          title={t('settings.agentBackendCodexNoticeTitle')}
          message={t('settings.agentBackendCodexNotice')}
          variant="warning"
        />
      ) : null}
      {error ? <ApiErrorAlert error={error} /> : null}
      {status ? (
        <div className="rounded-xl border settings-border bg-background/35 px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <StatusIcon status={status} />
            <span className="text-sm font-semibold text-foreground">
              {backendLabel(status.backend, t)}
            </span>
            <Badge variant={status.available ? 'success' : 'warning'} size="sm">
              {status.available
                ? t('settings.agentBackendCanTry')
                : t('settings.agentBackendNeedsAction')}
            </Badge>
            {status.experimental ? (
              <Badge variant="warning" size="sm">{t('settings.agentBackendExperimental')}</Badge>
            ) : null}
          </div>
          <p className="mt-2 text-xs leading-5 text-muted-text">{statusMessage(status, t)}</p>
          {(status.errorCode || status.version) ? (
            <details className="mt-3 text-xs text-muted-text">
              <summary className="cursor-pointer font-medium text-secondary-text">
                {t('settings.agentBackendTechnicalDetails')}
              </summary>
              <div className="mt-2 space-y-1 rounded-lg bg-background/50 px-3 py-2 font-mono text-[11px]">
                {status.version ? <p>{t('settings.agentBackendVersion')}: {status.version}</p> : null}
                {status.errorCode ? <p>{t('settings.agentBackendErrorCode')}: {status.errorCode}</p> : null}
              </div>
            </details>
          ) : null}
        </div>
      ) : null}

      {canManageAccount ? (
        <div data-testid="codex-account-control" className="rounded-xl border settings-border bg-background/35 px-4 py-3">
          <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
            <div>
              <p className="text-sm font-semibold text-foreground">{t('settings.codexAccountTitle')}</p>
              <p className="mt-1 text-xs leading-5 text-muted-text">{t('settings.codexAccountDescription')}</p>
            </div>
            <Button
              type="button"
              variant="settings-secondary"
              size="sm"
              disabled={accountLoading || accountActionLoading}
              isLoading={accountLoading}
              loadingText={t('settings.codexAccountRefreshing')}
              onClick={() => void refreshAccount()}
            >
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              {t('settings.codexAccountRefresh')}
            </Button>
          </div>

          {accountError ? <div className="mt-3"><ApiErrorAlert error={accountError} /></div> : null}
          {accountResponse ? (
            <div className="mt-3 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                {accountResponse.account.status === 'authenticated' ? (
                  <CheckCircle2 className="h-4 w-4 text-success" aria-hidden="true" />
                ) : (
                  <CircleAlert className="h-4 w-4 text-warning" aria-hidden="true" />
                )}
                <span className="text-sm font-semibold text-foreground">
                  {accountResponse.account.status === 'authenticated'
                    ? t('settings.codexAccountAuthenticated')
                    : t('settings.codexAccountSignedOut')}
                </span>
                {accountResponse.account.authMethod ? (
                  <Badge variant="default" size="sm">{accountResponse.account.authMethod}</Badge>
                ) : null}
                {accountResponse.account.planType ? (
                  <Badge variant="success" size="sm">{accountResponse.account.planType}</Badge>
                ) : null}
              </div>
              {accountResponse.account.email ? (
                <p className="text-xs text-muted-text">{accountResponse.account.email}</p>
              ) : null}
              {accountResponse.rateLimits?.snapshots.map((snapshot, snapshotIndex) => (
                <div key={`${snapshot.limitId || 'limit'}-${snapshotIndex}`} className="space-y-2 rounded-lg border settings-border px-3 py-2">
                  <p className="text-xs font-medium text-secondary-text">
                    {snapshot.limitName || snapshot.limitId || t('settings.codexAccountRateLimits')}
                  </p>
                  <div className="grid gap-2 text-xs text-muted-text sm:grid-cols-2">
                    {[snapshot.primary, snapshot.secondary].filter(Boolean).map((window) => (
                      <div key={`${snapshotIndex}-${window!.bucket}`}>
                        <span className="font-medium text-foreground">{window!.bucket}</span>
                        {` · ${window!.usedPercent}% · ${t('settings.codexAccountReset')}: ${formatResetTime(window!.resetsAt)}`}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
              {accountResponse.rateLimitErrorCode ? (
                <p className="text-xs text-muted-text">{t('settings.codexAccountRateLimitUnavailable')}</p>
              ) : null}
              {pendingLogin ? (
                <div className="space-y-2 rounded-lg border border-warning/40 bg-warning/5 px-3 py-2 text-xs text-muted-text">
                  <p className="font-medium text-foreground">{t('settings.codexAccountLoginPending')}</p>
                  {pendingLogin.authUrl ? (
                    <a className="text-link underline" href={pendingLogin.authUrl} target="_blank" rel="noreferrer">
                      {t('settings.codexAccountOpenBrowser')}
                    </a>
                  ) : null}
                  {pendingLogin.verificationUrl ? (
                    <a className="block text-link underline" href={pendingLogin.verificationUrl} target="_blank" rel="noreferrer">
                      {t('settings.codexAccountOpenVerification')}
                    </a>
                  ) : null}
                  {pendingLogin.userCode ? <p>{t('settings.codexAccountUserCode')}: <code>{pendingLogin.userCode}</code></p> : null}
                  <Button type="button" variant="settings-secondary" size="sm" disabled={accountActionLoading} onClick={() => void cancelLogin()}>
                    {t('settings.codexAccountCancelLogin')}
                  </Button>
                </div>
              ) : null}
              <div className="flex flex-wrap gap-2">
                {accountResponse.account.status !== 'authenticated' ? (
                  <>
                    <Button type="button" variant="settings-secondary" size="sm" disabled={accountActionLoading} onClick={() => void startLogin('browser')}>
                      {t('settings.codexAccountBrowserLogin')}
                    </Button>
                    <Button type="button" variant="settings-secondary" size="sm" disabled={accountActionLoading} onClick={() => void startLogin('device_code')}>
                      {t('settings.codexAccountDeviceLogin')}
                    </Button>
                  </>
                ) : (
                  <Button type="button" variant="settings-secondary" size="sm" disabled={accountActionLoading} onClick={() => void logout()}>
                    {t('settings.codexAccountLogout')}
                  </Button>
                )}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
