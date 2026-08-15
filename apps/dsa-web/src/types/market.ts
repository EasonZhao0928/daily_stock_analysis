/** Market workbench contracts returned by the chart/annotation API. */

export interface MarketCandle {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  amount?: number | null;
  changePercent?: number | null;
  indicators: {
    ma5?: number | null;
    ma10?: number | null;
    ma20?: number | null;
    rsi14?: number | null;
    macd?: number | null;
    macdSignal?: number | null;
    macdHistogram?: number | null;
  };
}

export interface MarketCandleResponse {
  securityId: Record<string, unknown>;
  period: string;
  candles: MarketCandle[];
  source: string;
  sourceStatus: string;
  fallbackChain: string[];
  asOf?: string | null;
  delaySeconds?: number | null;
  stale: boolean;
  dataQuality: string;
  limitations: string[];
}

export interface MarketAnnotation {
  type: string;
  source: string;
  timestamp?: string | null;
  accountId?: number | null;
  symbol: string;
  payload: Record<string, unknown>;
}

export interface MarketAnnotationResponse {
  symbol: string;
  start?: string | null;
  end?: string | null;
  items: MarketAnnotation[];
  page: number;
  pageSize: number;
  total: number;
  hasMore: boolean;
  partial: boolean;
  limitations: string[];
}
