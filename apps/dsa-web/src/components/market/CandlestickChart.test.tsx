import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { CandlestickChart } from './CandlestickChart';
import type { MarketCandle } from '../../types/market';

function candles(count = 100): MarketCandle[] {
  return Array.from({ length: count }, (_, index) => ({
    timestamp: `2026-08-${String((index % 28) + 1).padStart(2, '0')}T15:00:00+08:00-${index}`,
    open: 100 + index,
    high: 102 + index,
    low: 99 + index,
    close: 101 + index,
    volume: 1000 + index,
    indicators: {
      ma20: index >= 19 ? 91.5 + index : null,
      rsi14: 61.2,
      macd: 1.23,
      macdSignal: 1.0,
      macdHistogram: 0.46,
    },
  }));
}

describe('CandlestickChart', () => {
  it('supports keyboard-addressable zoom, indicators and an OHLC table fallback', () => {
    render(<CandlestickChart candles={candles()} stale />);

    expect(screen.getByText(/显示 80\/100 根/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '放大 K 线' }));
    expect(screen.getByText(/显示 60\/100 根/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /RSI/ }));
    expect(screen.getByText('61.2')).toBeInTheDocument();
    expect(screen.getByText('1.23')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /数据表/ }));
    expect(screen.getByRole('region', { name: 'K 线 OHLC 数据表' })).toBeInTheDocument();
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();
  });

  it('renders a clear empty state', () => {
    render(<CandlestickChart candles={[]} />);
    expect(screen.getByText('暂无 K 线')).toBeInTheDocument();
  });
});
