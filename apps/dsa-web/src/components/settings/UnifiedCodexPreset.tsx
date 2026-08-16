import type React from 'react';
import { Sparkles } from 'lucide-react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { Button } from '../common';
import { SettingsAlert } from './SettingsAlert';

interface UnifiedCodexPresetProps {
  active: boolean;
  disabled?: boolean;
  onApply: () => void;
}

/**
 * Draft-only convenience preset.  It writes the two existing backend keys;
 * it is intentionally not a third runtime switch.
 */
export const UnifiedCodexPreset: React.FC<UnifiedCodexPresetProps> = ({
  active,
  disabled = false,
  onApply,
}) => {
  const { t } = useUiLanguage();

  return (
    <div data-testid="unified-codex-preset" className="rounded-xl border settings-border bg-background/35 px-4 py-3">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Sparkles className="h-4 w-4 text-cyan" aria-hidden="true" />
            {t('settings.unifiedCodexTitle')}
          </p>
          <p className="mt-1 text-xs leading-5 text-muted-text">
            {t('settings.unifiedCodexDescription')}
          </p>
        </div>
        <Button
          type="button"
          variant={active ? 'settings-secondary' : 'settings-primary'}
          size="sm"
          disabled={disabled || active}
          onClick={onApply}
        >
          {active ? t('settings.unifiedCodexActive') : t('settings.unifiedCodexApply')}
        </Button>
      </div>
      <SettingsAlert
        className="mt-3"
        title={t('settings.unifiedCodexWarningTitle')}
        message={t('settings.unifiedCodexWarning')}
        variant="warning"
      />
    </div>
  );
};
