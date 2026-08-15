import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  MarketAnnotationResponse,
  MarketCandleResponse,
} from '../types/market';

export type MarketCandleQuery = {
  period?: '1d' | '1w' | '1m' | string;
  start?: string;
  end?: string;
  limit?: number;
};

export type MarketAnnotationQuery = {
  start?: string;
  end?: string;
  accountId?: number;
  page?: number;
  pageSize?: number;
};

function buildCandleParams(query: MarketCandleQuery = {}): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  if (query.period) params.period = query.period;
  if (query.start) params.start = query.start;
  if (query.end) params.end = query.end;
  if (query.limit != null) params.limit = query.limit;
  return params;
}

function buildAnnotationParams(query: MarketAnnotationQuery = {}): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  if (query.start) params.start = query.start;
  if (query.end) params.end = query.end;
  if (query.accountId != null) params.account_id = query.accountId;
  if (query.page != null) params.page = query.page;
  if (query.pageSize != null) params.page_size = query.pageSize;
  return params;
}

export const marketApi = {
  async getCandles(symbol: string, query: MarketCandleQuery = {}): Promise<MarketCandleResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/market/${encodeURIComponent(symbol.trim())}/candles`,
      { params: buildCandleParams(query) },
    );
    return toCamelCase<MarketCandleResponse>(response.data);
  },

  async getAnnotations(symbol: string, query: MarketAnnotationQuery = {}): Promise<MarketAnnotationResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/market/${encodeURIComponent(symbol.trim())}/annotations`,
      { params: buildAnnotationParams(query) },
    );
    return toCamelCase<MarketAnnotationResponse>(response.data);
  },

  streamUrl(symbol: string, query: { period?: string; intervalSeconds?: number } = {}): string {
    const params = new URLSearchParams();
    if (query.period) params.set('period', query.period);
    if (query.intervalSeconds != null) params.set('interval_seconds', String(query.intervalSeconds));
    const suffix = params.toString() ? `?${params.toString()}` : '';
    return `/api/v1/market/${encodeURIComponent(symbol.trim())}/stream${suffix}`;
  },
};
