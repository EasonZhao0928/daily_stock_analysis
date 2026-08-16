import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { UnifiedCodexPreset } from '../UnifiedCodexPreset';

describe('UnifiedCodexPreset', () => {
  it('writes through the existing backend selections only when applied', () => {
    const onApply = vi.fn();

    render(
      <UiLanguageProvider>
        <UnifiedCodexPreset active={false} onApply={onApply} />
      </UiLanguageProvider>,
    );

    const applyButton = screen.getByRole('button', { name: /Apply unified Codex|应用统一 Codex/ });
    expect(applyButton).toBeEnabled();
    fireEvent.click(applyButton);
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('unified-codex-preset')).toBeInTheDocument();
  });

  it('does not reapply an already active draft', () => {
    render(
      <UiLanguageProvider>
        <UnifiedCodexPreset active onApply={vi.fn()} />
      </UiLanguageProvider>,
    );

    expect(screen.getByRole('button', { name: /Applied to draft|已应用到草稿/ })).toBeDisabled();
  });
});
