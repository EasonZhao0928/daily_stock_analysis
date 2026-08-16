import { useCallback, useEffect, useRef, useState } from 'react';
import {
  agentApi,
  type CodexAccountLoginResponse,
  type CodexAccountStatusResponse,
  type CodexLoginMode,
} from '../../api/agent';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { CheckCircle2, CircleAlert, RefreshCw } from 'lucide-react';
import { ApiErrorAlert, Badge, Button } from '../common';

interface CodexAccountControlProps {
  enabled: boolean;
  disabled?: boolean;
}

function formatResetTime(timestamp: number | null): string {
  if (timestamp === null) return '—';
  const date = new Date(timestamp * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

/** Shared token-free Codex account/login UI for Generation and Agent settings. */
export function CodexAccountControl({ enabled, disabled = false }: CodexAccountControlProps) {
  const { t } = useUiLanguage();
  const [accountResponse, setAccountResponse] = useState<CodexAccountStatusResponse | null>(null);
  const [accountLoading, setAccountLoading] = useState(false);
  const [accountError, setAccountError] = useState<ParsedApiError | null>(null);
  const [accountActionLoading, setAccountActionLoading] = useState(false);
  const [pendingLogin, setPendingLogin] = useState<CodexAccountLoginResponse | null>(null);
  const accountRequestIdRef = useRef(0);

  const refreshAccount = useCallback(async () => {
    const requestId = accountRequestIdRef.current + 1;
    accountRequestIdRef.current = requestId;
    if (!enabled) {
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
  }, [enabled]);

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

  if (!enabled) {
    return null;
  }

  return (
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
          disabled={disabled || accountLoading || accountActionLoading}
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
              <Button type="button" variant="settings-secondary" size="sm" disabled={disabled || accountActionLoading} onClick={() => void cancelLogin()}>
                {t('settings.codexAccountCancelLogin')}
              </Button>
            </div>
          ) : null}
          <div className="flex flex-wrap gap-2">
            {accountResponse.account.status !== 'authenticated' ? (
              <>
                <Button type="button" variant="settings-secondary" size="sm" disabled={disabled || accountActionLoading} onClick={() => void startLogin('browser')}>
                  {t('settings.codexAccountBrowserLogin')}
                </Button>
                <Button type="button" variant="settings-secondary" size="sm" disabled={disabled || accountActionLoading} onClick={() => void startLogin('device_code')}>
                  {t('settings.codexAccountDeviceLogin')}
                </Button>
              </>
            ) : (
              <Button type="button" variant="settings-secondary" size="sm" disabled={disabled || accountActionLoading} onClick={() => void logout()}>
                {t('settings.codexAccountLogout')}
              </Button>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
