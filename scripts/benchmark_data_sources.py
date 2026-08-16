# -*- coding: utf-8 -*-
"""
===================================
数据源逐一连通性/成功率基准测试
===================================

背景：docs/data_source_architecture_roadmap.md 第 4 节（阶段 3：优先级哲学对齐）
需要在"目标部署环境"下拿到 PytdxFetcher/TencentFetcher 等各数据源的真实成功率和
延迟数据，才能决定是否把默认优先级调整为参考 a-stock-data 的"通达信/腾讯优先、
东财仅用于独有数据"。本脚本独立、逐一测试 data_provider 里的常驻 Fetcher（不经过
DataFetcherManager 的自动 fallback），这样某个源的失败不会被别的源掩盖。

同一份脚本可以分别在本地终端和服务器终端各跑一次，用 --label 区分产出文件，
再对比两份 JSON 结果里的 success_rate / avg_latency_ms，作为阶段 3 的决策依据。

使用方法：
    python scripts/benchmark_data_sources.py
    python scripts/benchmark_data_sources.py --label local-vpn
    python scripts/benchmark_data_sources.py --label server-cn
    python scripts/benchmark_data_sources.py --fetchers pytdx,tencent
    python scripts/benchmark_data_sources.py --codes 600519,000001,300750
    python scripts/benchmark_data_sources.py --repeat 2 --interval 2.0

参数：
    --fetchers  逗号分隔，默认全部 6 个常驻源：efinance,akshare,pytdx,baostock,yfinance,tencent
    --codes     逗号分隔股票代码，默认 5 只有代表性的 A 股（沪主板/深主板/创业板各一部分）
    --repeat    每个 (源, 代码) 组合重复请求次数，默认 1
    --interval  每次请求之间的固定间隔秒数，默认 1.5
    --days      每次请求的日线回看天数，默认 30
    --label     结果文件名标签，用来区分本地/服务器等不同环境的运行结果
    --output    结果 JSON 输出路径，默认写到 logs/benchmark_data_sources_<label>_<时间戳>.json

⚠️ 东财风控提醒（参考 docs/data_source_optimization_plan.md 附带的 a-stock-data 调研）：
东财系接口社区实测阈值约为 >5 次/秒高风险、5 分钟内 ≥300 次触发 IP 级封禁。本脚本默认
5 个代码 × 6 个源 × 1 次 = 30 次请求、间隔 1.5 秒，是刻意压低的安全值。调大 --repeat 或
调小 --interval 前，先确认不会让当次运行的总请求数逼近上述阈值。
"""

from __future__ import annotations

import os

# 代理处理方式与 main.py / scripts/check_env.py 保持一致：只有显式 USE_PROXY=true 才注入，
# 且沿用 src/config.py 里"配置了代理时自动把国内数据源域名加入 NO_PROXY"的既有逻辑。
if os.getenv("GITHUB_ACTIONS") != "true" and os.getenv("USE_PROXY", "false").lower() == "true":
    _proxy_host = os.getenv("PROXY_HOST", "127.0.0.1")
    _proxy_port = os.getenv("PROXY_PORT", "1082")
    _proxy_url = f"http://{_proxy_host}:{_proxy_port}"
    os.environ["http_proxy"] = _proxy_url
    os.environ["https_proxy"] = _proxy_url

import argparse
import json
import platform
import socket
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import setup_env  # noqa: E402

setup_env()

DEFAULT_CODES = ["600519", "000001", "300750", "002594", "600000"]
DEFAULT_FETCHERS = ["efinance", "akshare", "pytdx", "baostock", "yfinance", "tencent"]

# 每个常驻 Fetcher 对应的模块/类名，逐一独立实例化，不经过 DataFetcherManager 的自动
# fallback——这样才能拿到每个源"自己"的成功率，而不是被别的源兜底掩盖的整体成功率。
FETCHER_IMPORTS: Dict[str, tuple[str, str]] = {
    "efinance": ("data_provider.efinance_fetcher", "EfinanceFetcher"),
    "akshare": ("data_provider.akshare_fetcher", "AkshareFetcher"),
    "pytdx": ("data_provider.pytdx_fetcher", "PytdxFetcher"),
    "baostock": ("data_provider.baostock_fetcher", "BaostockFetcher"),
    "yfinance": ("data_provider.yfinance_fetcher", "YfinanceFetcher"),
    "tencent": ("data_provider.tencent_fetcher", "TencentFetcher"),
}


@dataclass
class Attempt:
    fetcher: str
    code: str
    attempt_no: int
    success: bool
    rows: int = 0
    elapsed_ms: float = 0.0
    error_type: str = ""
    error_message: str = ""


@dataclass
class FetcherSummary:
    fetcher: str
    total_attempts: int = 0
    success_count: int = 0
    failure_count: int = 0
    success_rate: float = 0.0
    avg_latency_ms: Optional[float] = None
    min_latency_ms: Optional[float] = None
    max_latency_ms: Optional[float] = None
    error_types: Dict[str, int] = field(default_factory=dict)
    instantiate_failed: bool = False
    instantiate_error: str = ""


def build_fetcher(name: str):
    module_name, class_name = FETCHER_IMPORTS[name]
    import importlib

    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls()


def run_single_attempt(fetcher_name: str, fetcher_instance: Any, code: str, attempt_no: int, days: int) -> Attempt:
    start = time.monotonic()
    try:
        df = fetcher_instance.get_daily_data(code, days=days)
        elapsed_ms = (time.monotonic() - start) * 1000
        rows = 0 if df is None else len(df)
        return Attempt(
            fetcher=fetcher_name,
            code=code,
            attempt_no=attempt_no,
            success=rows > 0,
            rows=rows,
            elapsed_ms=round(elapsed_ms, 1),
        )
    except Exception as exc:  # noqa: BLE001 - 基准脚本需要捕获任意上游异常并记录
        elapsed_ms = (time.monotonic() - start) * 1000
        return Attempt(
            fetcher=fetcher_name,
            code=code,
            attempt_no=attempt_no,
            success=False,
            rows=0,
            elapsed_ms=round(elapsed_ms, 1),
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )


def summarize(fetcher_name: str, attempts: List[Attempt]) -> FetcherSummary:
    summary = FetcherSummary(fetcher=fetcher_name, total_attempts=len(attempts))
    if not attempts:
        return summary
    successes = [a for a in attempts if a.success]
    failures = [a for a in attempts if not a.success]
    summary.success_count = len(successes)
    summary.failure_count = len(failures)
    summary.success_rate = round(len(successes) / len(attempts), 4)
    if successes:
        latencies = [a.elapsed_ms for a in successes]
        summary.avg_latency_ms = round(statistics.mean(latencies), 1)
        summary.min_latency_ms = round(min(latencies), 1)
        summary.max_latency_ms = round(max(latencies), 1)
    for a in failures:
        key = a.error_type or "unknown"
        summary.error_types[key] = summary.error_types.get(key, 0) + 1
    return summary


def collect_environment_metadata(label: str) -> Dict[str, Any]:
    proxy_active = os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""
    no_proxy = os.getenv("NO_PROXY") or os.getenv("no_proxy") or ""
    try:
        import akshare  # noqa: WPS433

        akshare_version = getattr(akshare, "__version__", "unknown")
    except Exception:
        akshare_version = "not_installed"
    return {
        "label": label,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "akshare_version": akshare_version,
        "proxy_active": bool(proxy_active),
        "proxy_url_present": bool(proxy_active),
        "no_proxy": no_proxy,
        "run_started_at": datetime.now(timezone.utc).isoformat(),
    }


def print_table(summaries: List[FetcherSummary]) -> None:
    header = f"{'Fetcher':<12}{'成功/总数':<10}{'成功率':<8}{'均延迟(ms)':<12}{'最小/最大(ms)':<18}{'主要错误':<30}"
    print(header)
    print("-" * len(header))
    for s in summaries:
        if s.instantiate_failed:
            print(f"{s.fetcher:<12}{'--':<10}{'--':<8}{'--':<12}{'--':<18}实例化失败: {s.instantiate_error[:60]}")
            continue
        ratio = f"{s.success_count}/{s.total_attempts}"
        rate = f"{s.success_rate * 100:.0f}%"
        avg_lat = f"{s.avg_latency_ms}" if s.avg_latency_ms is not None else "--"
        minmax = f"{s.min_latency_ms}/{s.max_latency_ms}" if s.min_latency_ms is not None else "--"
        top_error = ""
        if s.error_types:
            top_error = max(s.error_types.items(), key=lambda kv: kv[1])
            top_error = f"{top_error[0]}×{top_error[1]}"
        print(f"{s.fetcher:<12}{ratio:<10}{rate:<8}{avg_lat:<12}{minmax:<18}{top_error:<30}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="逐一测试 data_provider 常驻数据源的日线获取成功率与延迟（阶段 3 决策依据）",
    )
    parser.add_argument("--fetchers", default=",".join(DEFAULT_FETCHERS), help="逗号分隔的源名称")
    parser.add_argument("--codes", default=",".join(DEFAULT_CODES), help="逗号分隔的股票代码")
    parser.add_argument("--repeat", type=int, default=1, help="每个 (源, 代码) 组合的重复次数")
    parser.add_argument("--interval", type=float, default=1.5, help="每次请求之间的固定间隔秒数")
    parser.add_argument("--days", type=int, default=30, help="每次请求的日线回看天数")
    parser.add_argument("--label", default="local", help="结果标签（如 local-vpn / server-cn），用于区分产出文件")
    parser.add_argument("--output", default="", help="结果 JSON 输出路径；留空则自动写到 logs/ 下")
    args = parser.parse_args()

    fetcher_names = [f.strip() for f in args.fetchers.split(",") if f.strip()]
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    unknown = [f for f in fetcher_names if f not in FETCHER_IMPORTS]
    if unknown:
        print(f"未知数据源: {unknown}；可选值: {sorted(FETCHER_IMPORTS)}", file=sys.stderr)
        return 2

    total_requests = len(fetcher_names) * len(codes) * args.repeat
    print(f"计划请求总数: {total_requests}（{len(fetcher_names)} 源 × {len(codes)} 代码 × {args.repeat} 次），"
          f"间隔 {args.interval}s，预计耗时 >= {total_requests * args.interval:.0f}s")
    if total_requests > 150:
        print("⚠️ 请求总数偏高，注意东财风控阈值（社区实测 5 分钟内 ≥300 次触发封禁），"
              "建议减少 --repeat 或 --codes 数量。", file=sys.stderr)

    env_meta = collect_environment_metadata(args.label)
    print(f"运行环境: {env_meta['hostname']} / {env_meta['platform']} / "
          f"代理={'开' if env_meta['proxy_active'] else '关'}")

    all_attempts: List[Attempt] = []
    summaries: List[FetcherSummary] = []

    for fetcher_name in fetcher_names:
        print(f"\n=== {fetcher_name} ===")
        try:
            fetcher_instance = build_fetcher(fetcher_name)
        except Exception as exc:  # noqa: BLE001
            print(f"  实例化失败: {exc}")
            summaries.append(
                FetcherSummary(fetcher=fetcher_name, instantiate_failed=True, instantiate_error=str(exc)[:200])
            )
            continue

        fetcher_attempts: List[Attempt] = []
        for code in codes:
            for attempt_no in range(1, args.repeat + 1):
                attempt = run_single_attempt(fetcher_name, fetcher_instance, code, attempt_no, args.days)
                fetcher_attempts.append(attempt)
                all_attempts.append(attempt)
                status = "OK" if attempt.success else f"FAIL({attempt.error_type or 'empty'})"
                print(f"  {code} #{attempt_no}: {status} {attempt.elapsed_ms:.0f}ms rows={attempt.rows}")
                time.sleep(args.interval)

        summaries.append(summarize(fetcher_name, fetcher_attempts))

    print("\n" + "=" * 90)
    print("汇总")
    print("=" * 90)
    print_table(summaries)

    output_path = Path(args.output) if args.output else None
    if output_path is None:
        logs_dir = REPO_ROOT / "logs"
        logs_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = args.label.replace("/", "_") or "run"
        output_path = logs_dir / f"benchmark_data_sources_{safe_label}_{timestamp}.json"

    result_payload = {
        "environment": env_meta,
        "params": {
            "fetchers": fetcher_names,
            "codes": codes,
            "repeat": args.repeat,
            "interval": args.interval,
            "days": args.days,
        },
        "summaries": [asdict(s) for s in summaries],
        "attempts": [asdict(a) for a in all_attempts],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入: {output_path}")
    print("把本地和服务器各跑一次的 JSON 放在一起对比 summaries[].success_rate / avg_latency_ms，"
          "作为 docs/data_source_architecture_roadmap.md 阶段 3 的决策依据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
