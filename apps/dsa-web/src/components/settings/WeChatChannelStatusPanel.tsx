import { useCallback, useEffect, useState } from 'react';
import type React from 'react';
import { RefreshCw } from 'lucide-react';
import { systemConfigApi } from '../../api/systemConfig';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { WeChatChannelStatusResponse } from '../../types/systemConfig';
import { Badge, Button, InlineAlert, Loading } from '../common';
import { SettingsSectionCard } from './SettingsSectionCard';

interface WeChatChannelStatusPanelProps {
  disabled?: boolean;
}

export const WeChatChannelStatusPanel: React.FC<WeChatChannelStatusPanelProps> = ({ disabled = false }) => {
  const { t } = useUiLanguage();
  const [status, setStatus] = useState<WeChatChannelStatusResponse | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await systemConfigApi.getWeChatChannelStatus());
    } catch (caught) {
      setError(getParsedApiError(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <SettingsSectionCard
      title={t('settings.wechatPersonalTitle')}
      description={t('settings.wechatPersonalDescription')}
      actions={(
        <Button
          type="button"
          variant="settings-secondary"
          size="sm"
          onClick={() => void refresh()}
          disabled={disabled || loading}
          isLoading={loading}
          loadingText={t('settings.wechatPersonalRefresh')}
        >
          <RefreshCw className="h-4 w-4" />
          {t('settings.wechatPersonalRefresh')}
        </Button>
      )}
    >
      {error ? <InlineAlert variant="danger" title={t('settings.wechatPersonalStatusError')} message={error.message} /> : null}
      {loading && !status ? <Loading label={t('common.loading')} /> : null}
      {status ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={status.ready ? 'success' : 'warning'}>
              {status.ready ? t('settings.wechatPersonalConfigured') : t('settings.wechatPersonalNotReady')}
            </Badge>
            <span className="text-sm text-secondary-text">{status.message}</span>
          </div>
          <div className="grid grid-cols-2 gap-3 text-xs text-secondary-text sm:grid-cols-4">
            <StatusItem label={t('settings.wechatPersonalEnabled')} value={status.enabled ? t('common.enabled') : t('common.disabled')} />
            <StatusItem label={t('settings.wechatPersonalBaseUrl')} value={status.baseUrlConfigured ? t('settings.wechatPersonalConfigured') : t('settings.wechatPersonalMissing')} />
            <StatusItem label={t('settings.wechatPersonalCredential')} value={status.credentialConfigured ? t('settings.wechatPersonalConfigured') : t('settings.wechatPersonalMissing')} />
            <StatusItem label={t('settings.wechatPersonalAllowlist')} value={String(status.allowlistCount)} />
          </div>
        </div>
      ) : null}
    </SettingsSectionCard>
  );
};

const StatusItem: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div>
    <p>{label}</p>
    <p className="mt-1 font-medium text-foreground">{value}</p>
  </div>
);
