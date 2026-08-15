import type React from 'react';
import { useMemo, useState } from 'react';
import { Activity, BarChart3, Eye, EyeOff, Minus, Plus, RotateCcw, Table2, TrendingUp } from 'lucide-react';
import { Badge, EmptyState } from '../common';
import type { MarketAnnotation, MarketCandle, MarketCandleResponse } from '../../types/market';

type CandlestickChartProps = {
  candles: MarketCandle[];
  annotations?: MarketAnnotation[];
  stale?: boolean;
  metadata?: Pick<MarketCandleResponse, 'source' | 'asOf' | 'delaySeconds' | 'dataQuality' | 'limitations' | 'fallbackChain'>;
  className?: string;
};

type ChartPoint = { x: number; y: number };

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

function movingAverage(values: number[], windowSize: number): Array<number | null> {
  return values.map((_, index) => {
    if (index + 1 < windowSize) return null;
    const window = values.slice(index + 1 - windowSize, index + 1);
    return window.reduce((sum, value) => sum + value, 0) / window.length;
  });
}

function relativeStrengthIndex(values: number[], windowSize = 14): number | null {
  if (values.length <= windowSize) return null;
  const changes = values.slice(1).map((value, index) => value - values[index]);
  const window = changes.slice(-windowSize);
  const gains = window.filter((value) => value > 0).reduce((sum, value) => sum + value, 0) / windowSize;
  const losses = Math.abs(window.filter((value) => value < 0).reduce((sum, value) => sum + value, 0)) / windowSize;
  if (losses === 0) return gains > 0 ? 100 : 50;
  return 100 - 100 / (1 + gains / losses);
}

function exponentialMovingAverage(values: number[], span: number): number[] {
  const alpha = 2 / (span + 1);
  return values.reduce<number[]>((output, value) => {
    output.push(output.length ? alpha * value + (1 - alpha) * output[output.length - 1] : value);
    return output;
  }, []);
}

function macdSeries(values: number[]): number[] {
  const fast = exponentialMovingAverage(values, 12);
  const slow = exponentialMovingAverage(values, 26);
  return fast.map((value, index) => value - slow[index]);
}

function toChartPoint(index: number, value: number, min: number, range: number, count: number, top = 8, bottom = 72): ChartPoint {
  return {
    x: count === 1 ? 50 : 4 + (index / (count - 1)) * 92,
    y: bottom - ((value - min) / range) * (bottom - top),
  };
}

function pointsToString(points: Array<ChartPoint | null>): string {
  return points.filter((point): point is ChartPoint => Boolean(point)).map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
}

function annotationColor(item: MarketAnnotation): string {
  if (item.source.includes('paper') || item.type.startsWith('paper') || item.type.startsWith('virtual')) return '#22d3ee';
  if (item.source.includes('shadow') || item.type.startsWith('shadow')) return '#a78bfa';
  if (item.source.includes('alert') || item.type.startsWith('alert')) return '#f59e0b';
  return '#4ade80';
}

const controlClass = 'inline-flex h-11 min-w-11 items-center justify-center gap-1 rounded-lg border border-border/60 px-3 text-xs text-secondary-text transition-colors hover:bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan/50 disabled:cursor-not-allowed disabled:opacity-40';

export const CandlestickChart: React.FC<CandlestickChartProps> = ({ candles, annotations = [], stale = false, metadata, className = '' }) => {
  const [showVolume, setShowVolume] = useState(true);
  const [showMa, setShowMa] = useState(true);
  const [showRsi, setShowRsi] = useState(false);
  const [showTable, setShowTable] = useState(false);
  const [visibleCount, setVisibleCount] = useState(80);
  const minimumVisible = Math.min(20, Math.max(1, candles.length));
  const normalizedVisibleCount = clamp(visibleCount, minimumVisible, Math.max(minimumVisible, candles.length));
  const visibleCandles = useMemo(() => candles.slice(-normalizedVisibleCount), [candles, normalizedVisibleCount]);
  const closes = useMemo(() => visibleCandles.map((candle) => Number(candle.close)), [visibleCandles]);
  const fallbackMa = useMemo(() => movingAverage(closes, Math.min(20, Math.max(2, Math.floor(closes.length / 4) || 2))), [closes]);
  const ma = useMemo(() => visibleCandles.map((candle, index) => candle.indicators?.ma20 ?? fallbackMa[index]), [fallbackMa, visibleCandles]);
  const fallbackMacd = useMemo(() => macdSeries(closes), [closes]);
  const rsi = visibleCandles.at(-1)?.indicators?.rsi14 ?? relativeStrengthIndex(closes);
  const macd = visibleCandles.at(-1)?.indicators?.macd ?? fallbackMacd.at(-1) ?? null;
  const bounds = useMemo(() => {
    const prices = visibleCandles.flatMap((candle) => [Number(candle.high), Number(candle.low)]).filter(Number.isFinite);
    const min = prices.length ? Math.min(...prices) : 0;
    const max = prices.length ? Math.max(...prices) : 1;
    return { min, range: Math.max(max - min, Math.abs(max) * 0.0001, 0.000001), maxVolume: Math.max(...visibleCandles.map((candle) => Number(candle.volume) || 0), 1) };
  }, [visibleCandles]);
  const annotationIndex = useMemo(() => {
    const lookup = new Map<string, number>();
    visibleCandles.forEach((candle, index) => lookup.set(String(candle.timestamp).slice(0, 10), index));
    return lookup;
  }, [visibleCandles]);

  if (!candles.length) {
    return <EmptyState title="暂无 K 线" description="输入股票代码并刷新行情后，这里会显示归一化的 OHLC 数据。" icon={<Activity className="h-6 w-6" />} />;
  }

  const latest = visibleCandles[visibleCandles.length - 1];
  const previous = visibleCandles[visibleCandles.length - 2];
  const change = latest.changePercent ?? (previous ? ((latest.close - previous.close) / previous.close) * 100 : null);
  const candleWidth = Math.max(1.2, Math.min(5, 86 / visibleCandles.length));
  const chartHeight = showVolume ? 112 : 92;
  const priceBottom = showVolume ? 74 : 88;
  const maPoints = showMa ? ma.map((value, index) => (value == null ? null : toChartPoint(index, value, bounds.min, bounds.range, visibleCandles.length, 8, priceBottom))) : [];

  return (
    <div className={`space-y-3 ${className}`}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-secondary-text">最新收盘</p>
          <p className="mt-1 text-2xl font-semibold text-foreground">{latest.close.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</p>
        </div>
        <div className="flex items-center gap-2">
          {stale ? <Badge variant="warning">stale</Badge> : null}
          <Badge variant={change != null && change >= 0 ? 'success' : 'danger'}>
            <TrendingUp className="h-3.5 w-3.5" />
            {change == null ? '--' : `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`}
          </Badge>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <button type="button" className={controlClass} onClick={() => setShowVolume((value) => !value)} aria-pressed={showVolume}>
          {showVolume ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}成交量
        </button>
        <button type="button" className={controlClass} onClick={() => setShowMa((value) => !value)} aria-pressed={showMa}>
          {showMa ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}MA
        </button>
        <button type="button" className={controlClass} onClick={() => setShowRsi((value) => !value)} aria-pressed={showRsi}>
          {showRsi ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}RSI
        </button>
        <button type="button" className={controlClass} onClick={() => setVisibleCount((value) => Math.max(minimumVisible, value - 20))} disabled={normalizedVisibleCount <= minimumVisible} aria-label="放大 K 线">
          <Plus className="h-3.5 w-3.5" />放大
        </button>
        <button type="button" className={controlClass} onClick={() => setVisibleCount((value) => Math.min(candles.length, value + 20))} disabled={normalizedVisibleCount >= candles.length} aria-label="缩小 K 线">
          <Minus className="h-3.5 w-3.5" />缩小
        </button>
        <button type="button" className={controlClass} onClick={() => setVisibleCount(candles.length)} aria-label="重置 K 线缩放">
          <RotateCcw className="h-3.5 w-3.5" />重置
        </button>
        <button type="button" className={controlClass} onClick={() => setShowTable((value) => !value)} aria-pressed={showTable}>
          <Table2 className="h-3.5 w-3.5" />数据表
        </button>
        <span className="ml-auto text-secondary-text">显示 {visibleCandles.length}/{candles.length} 根 · {String(latest.timestamp).slice(0, 10)}</span>
      </div>
      {metadata ? <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-secondary-text" aria-label="行情来源元数据">
        <span>来源：{metadata.source || '--'}</span>
        <span>as of：{metadata.asOf ? String(metadata.asOf).replace('T', ' ').slice(0, 19) : '--'}</span>
        <span>延迟：{metadata.delaySeconds == null ? '--' : `${Math.round(metadata.delaySeconds)}s`}</span>
        <span>quality：{metadata.dataQuality || '--'}</span>
        {metadata.fallbackChain?.length ? <span className="text-warning">fallback：{metadata.fallbackChain.join(' → ')}</span> : null}
      </div> : null}
      <div className="overflow-hidden rounded-2xl border border-border/60 bg-black/10 p-2">
        <svg viewBox={`0 0 100 ${chartHeight}`} className="h-56 w-full" role="img" aria-label="OHLC K 线与成交量预览">
          <path d={`M0 ${priceBottom}H100M0 ${Math.round(priceBottom / 2)}H100M0 8H100`} stroke="currentColor" strokeOpacity="0.1" strokeWidth="0.5" />
          {visibleCandles.map((candle, index) => {
            const x = visibleCandles.length === 1 ? 50 : 4 + (index / (visibleCandles.length - 1)) * 92;
            const open = toChartPoint(index, Number(candle.open), bounds.min, bounds.range, visibleCandles.length, 8, priceBottom);
            const close = toChartPoint(index, Number(candle.close), bounds.min, bounds.range, visibleCandles.length, 8, priceBottom);
            const high = toChartPoint(index, Number(candle.high), bounds.min, bounds.range, visibleCandles.length, 8, priceBottom);
            const low = toChartPoint(index, Number(candle.low), bounds.min, bounds.range, visibleCandles.length, 8, priceBottom);
            const up = Number(candle.close) >= Number(candle.open);
            const color = up ? '#4ade80' : '#fb7185';
            const bodyY = Math.min(open.y, close.y);
            const bodyHeight = Math.max(0.8, Math.abs(open.y - close.y));
            const volume = Number(candle.volume) || 0;
            const volumeHeight = showVolume ? clamp((volume / bounds.maxVolume) * 25, 0.5, 25) : 0;
            return <g key={`${candle.timestamp}-${index}`}>
              <line x1={x} x2={x} y1={high.y} y2={low.y} stroke={color} strokeWidth="0.7" />
              <rect x={x - candleWidth / 2} y={bodyY} width={candleWidth} height={bodyHeight} fill={color} fillOpacity={up ? 0.28 : 0.82} stroke={color} strokeWidth="0.35" rx="0.35" />
              {showVolume ? <rect x={x - candleWidth / 2} y={chartHeight - volumeHeight - 2} width={candleWidth} height={volumeHeight} fill={color} fillOpacity="0.25" rx="0.2" /> : null}
            </g>;
          })}
          {showMa ? <polyline fill="none" points={pointsToString(maPoints)} stroke="#fbbf24" strokeLinecap="round" strokeLinejoin="round" strokeWidth="0.9" /> : null}
          {annotations.map((item, index) => {
            const candleIndex = annotationIndex.get(String(item.timestamp ?? '').slice(0, 10));
            if (candleIndex == null) return null;
            const x = visibleCandles.length === 1 ? 50 : 4 + (candleIndex / (visibleCandles.length - 1)) * 92;
            const y = 5 + (index % 3) * 4;
            return <circle key={`${item.type}-${item.timestamp}-${index}`} cx={x} cy={y} r="1.5" fill={annotationColor(item)} aria-label={`${item.source} ${item.type}`} />;
          })}
        </svg>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs text-secondary-text sm:grid-cols-4">
        <span>开 {latest.open.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</span>
        <span>高 {latest.high.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</span>
        <span>低 {latest.low.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}</span>
        <span>量 {(latest.volume ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}</span>
      </div>
      {showRsi ? <div className="flex flex-wrap items-center gap-3 text-xs text-secondary-text"><Activity className="h-3.5 w-3.5 text-cyan" />RSI(14) <span className="font-mono text-foreground">{rsi == null ? '--' : rsi.toFixed(1)}</span><BarChart3 className="ml-2 h-3.5 w-3.5 text-purple" />MACD <span className="font-mono text-foreground">{macd == null ? '--' : macd.toFixed(2)}</span></div> : null}
      {showTable ? <div className="max-h-72 overflow-auto rounded-xl border border-border/60" role="region" aria-label="K 线 OHLC 数据表" tabIndex={0}>
        <table className="w-full min-w-[36rem] text-left text-xs">
          <thead className="sticky top-0 bg-card text-secondary-text"><tr><th className="p-2">日期</th><th className="p-2">开</th><th className="p-2">高</th><th className="p-2">低</th><th className="p-2">收</th><th className="p-2">量</th></tr></thead>
          <tbody>{visibleCandles.slice(-100).map((candle) => <tr key={`table-${candle.timestamp}`} className="border-t border-border/40"><td className="p-2">{String(candle.timestamp).slice(0, 10)}</td><td className="p-2">{candle.open}</td><td className="p-2">{candle.high}</td><td className="p-2">{candle.low}</td><td className="p-2">{candle.close}</td><td className="p-2">{candle.volume ?? '--'}</td></tr>)}</tbody>
        </table>
      </div> : null}
      {metadata?.limitations?.length ? <p className="text-xs text-warning">限制：{metadata.limitations.join('；')}</p> : null}
    </div>
  );
};
