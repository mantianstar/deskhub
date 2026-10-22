"""配置加载与校验。

全局唯一配置入口：启动时一次性加载，校验失败直接抛 ConfigError（由调用方退出进程）。

覆盖优先级：环境变量 > .env > config.yaml
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"

# 允许用环境变量覆盖的键（其余键只走 config.yaml）
ENV_LLM_BASE_URL = "DESKHUB_LLM_BASE_URL"
ENV_LLM_MODEL = "DESKHUB_LLM_MODEL"
ENV_DB_PATH = "DESKHUB_DB_PATH"

VALID_MODULES = ("agent", "bigdata")


class ConfigError(Exception):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    timezone: str
    db_path: Path
    log_path: Path

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class DigestConfig:
    limit: int
    lookback_hours: int


@dataclass(frozen=True)
class FetchConfig:
    timeout_seconds: int
    user_agent: str
    max_items_per_source: int
    max_concurrency: int


@dataclass(frozen=True)
class ScoringConfig:
    concurrency: int
    max_summary_chars: int
    max_attempts: int
    prompt_version: str
    temperature: float


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key_env: str
    api_key: str
    # 单次请求超时（秒）。批量抽取画像要久，30s 不够；默认给宽一点
    timeout_seconds: int = 120


@dataclass(frozen=True)
class ModuleConfig:
    key: str
    label: str
    focus: str


@dataclass(frozen=True)
class CoinPriceConfig:
    """推荐价的取数口径（spec §14.6）。"""

    status: str
    window_days: int
    min_samples: int
    statistic: str
    # 成交价接口要**登录**才能用 —— 也就是拿我自己的账号在查，所以宁可慢：
    # 每次查价之间停这么久（带 ±40% 抖动，避免固定节奏），单轮最多查这么多条。
    # 剩下的留到下一轮/第二天（用户 2026-10-12 明确要求：不要求效率，别被封号）。
    query_interval_seconds: int = 10
    max_queries_per_run: int = 30


@dataclass(frozen=True)
class CoinProfileLLMConfig:
    prompt_version: str
    batch_size: int
    concurrency: int = 4


@dataclass(frozen=True)
class CoinNumistaConfig:
    """Numista 目录接口（铜币参考库，spec §14.13 / plan §18.13）。

    风控（2026-10-14 用户明确）：免费额度每月 2000 次，**响应不带限流头**，只能自管：
    `request_interval_seconds` 拉大间隔、`max_requests_per_run` 限单轮、`monthly_limit` 限月度总额。
    """

    base_url: str
    api_key_env: str
    api_key: str
    # 请求间隔（秒）：免费额度有限，对接口客气点
    request_interval_seconds: int = 2
    # 单次搜索最多取几个系列的详情（Numista 每个搜索有「Type Detail 请求」上限，取个保守值）
    max_series: int = 10
    timeout_seconds: int = 20
    # 每月请求上限（免费额度 2000，留 25% 余量）
    monthly_limit: int = 1500
    # 单轮请求上限（一次 CLI 调用最多打几次接口）
    max_requests_per_run: int = 40
    # 取类型页（估值 + Frequency）时挂载的 CDP 浏览器地址：Numista 上有 Cloudflare 校验，
    # 普通 HTTP 客户端过不去，只能挂到你自己用 --remote-debugging-port 开的 Chrome 上
    cdp_url: str = "http://127.0.0.1:9222"


@dataclass(frozen=True)
class ExploreCatalogueConfig:
    """探索候选池的目录筛选（Numista catalogue/index.php 的查询参数）。

    这些值就是实际拼进 URL 的参数，后续换筛选条件只改 `config.yaml` 里的对应项：
    `collectible_type` → `st`（1 = Standard circulation coins）、`year` → `a`、
    `composition_id` → `m`（5 = Bronze）、`diameter_mm` → `t`、`per_page` → `q`。
    """

    collectible_type: int = 1
    year: str = "1800-2026"
    composition_id: int = 5
    diameter_mm: str = "28-33"
    per_page: int = 200


@dataclass(frozen=True)
class CoinCollectionConfig:
    """收藏画像与推荐优先级的口径（spec §14.17）。"""

    # 前两层推荐总数少于此值时，才触发相似推荐（P4 年份匹配 / P5 面值探索）
    min_results: int = 5
    # 稀缺权重 = 1 / max(rarity, rarity_floor)；rarity 是 Numista Frequency / 100
    rarity_floor: float = 0.005
    # 每个收藏系列的升级项最多取几个
    top_upgrades_per_series: int = 3
    # 面值层级（跨系列相似推荐用）：同一组里的面值视为同一层级
    denomination_tiers: tuple[tuple[str, ...], ...] = ()
    # 目录扫描（候选池）：尺寸差容差、单轮最多取几个候选详情、目录筛选参数
    explore_size_tolerance_mm: float = 2.0
    explore_max_details: int = 10
    explore_catalogue: ExploreCatalogueConfig = ExploreCatalogueConfig()


@dataclass(frozen=True)
class CoinsConfig:
    """铜币拍卖（spec §14 / plan §18.10）。"""

    enabled: bool
    orders_xlsx: Path
    request_interval_seconds: int
    max_sessions_per_run: int
    match_threshold: float
    price: CoinPriceConfig
    profile_llm: CoinProfileLLMConfig
    # 小程序网关：采集（专场 + 拍品）与查价（站内历史成交）的**唯一**来源
    # （免登录、免签名、免浏览器，见 app/coins/gapi.py）
    gapi_base_url: str = "https://gapi.mxiqi.com"
    # Numista 目录接口（参考库，见 app/coins/numista.py）
    numista: CoinNumistaConfig = field(
        default_factory=lambda: CoinNumistaConfig(
            base_url="https://api.numista.com/v3",
            api_key_env="DESKHUB_NUMISTA_API_KEY",
            api_key="",
        )
    )
    # 收藏画像与推荐优先级（spec §14.17）
    collection: CoinCollectionConfig = field(default_factory=CoinCollectionConfig)
    # 终态回采窗口：回看最近 N 天已结束的专场（0 = 不回采，见 plan/coins.md §18.18）
    finalize_days: int = 1


def _default_coins_config() -> CoinsConfig:
    """`Config` 手工构造时的兜底值（测试用）。

    生产走 `config.load()`，一定会由 `_parse_coins` 显式构造；这里只是让
    fetcher / scorer / scheduler 的测试不必为无关的铜币模块写样板。
    """
    return CoinsConfig(
        enabled=False,
        orders_xlsx=PROJECT_ROOT / "data" / "orders.xlsx",
        request_interval_seconds=0,
        max_sessions_per_run=5,
        match_threshold=1.5,
        price=CoinPriceConfig(status="成交", window_days=180, min_samples=3, statistic="median",
                              query_interval_seconds=0, max_queries_per_run=5),
        profile_llm=CoinProfileLLMConfig(prompt_version="coins-v1", batch_size=2, concurrency=1),
        finalize_days=1,
    )


@dataclass(frozen=True)
class SourceConfig:
    name: str
    url: str
    module: str
    enabled: bool
    published_tz: str | None = None

    @property
    def tz(self) -> ZoneInfo | None:
        """源声明的「feed 时间戳所在时区」；未声明返回 None（按 feed 自己标的时区走）。

        只有源把本地时间标错成 GMT/UTC 时才需要声明它（见 plan §7.2 补充）。
        """
        return ZoneInfo(self.published_tz) if self.published_tz else None


@dataclass(frozen=True)
class Config:
    app: AppConfig
    digest: DigestConfig
    fetch: FetchConfig
    scoring: ScoringConfig
    llm: LLMConfig
    modules: dict[str, ModuleConfig]
    sources: tuple[SourceConfig, ...]
    # 铜币配置只对铜币模块有意义；给个兜底，让「手工构造 Config」的测试（fetcher/scorer/
    # scheduler）不必为无关模块写样板。生产一律走 config.load()，会显式传入。
    coins: CoinsConfig = field(default_factory=lambda: _default_coins_config())

    def module(self, key: str) -> ModuleConfig:
        try:
            return self.modules[key]
        except KeyError:
            raise ConfigError(f"未知 module：{key}（可选：{', '.join(self.modules)}）") from None

    def source_for(self, url: str) -> SourceConfig | None:
        """按 feed url 取源配置。

        `sources` 表里只有运行状态，`published_tz` 这类配置项不落库（plan §5.2 的源清单是唯一真源），
        所以抓取时靠 url 回配置里取。
        """
        return next((source for source in self.sources if source.url == url), None)


_config: Config | None = None


# ---------------------------------------------------------------- 取值工具


def _section(data: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{where}：缺少 `{key}` 配置段")
    return value


def _req_str(section: dict[str, Any], key: str, where: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key}：必须是非空字符串")
    return value.strip()


def _opt_str(section: dict[str, Any], key: str, default: str, where: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key}：必须是非空字符串")
    return value.strip()


def _req_int(section: dict[str, Any], key: str, where: str, *, minimum: int = 1) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key}：必须是整数")
    if value < minimum:
        raise ConfigError(f"{where}.{key}：必须 >= {minimum}，当前 {value}")
    return value


def _require_float(section: dict[str, Any], key: str, where: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key}：必须是数字")
    return float(value)


def _opt_bool(section: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}：必须是 true/false")
    return value


def _opt_int(section: dict[str, Any], key: str, default: int, where: str, *, minimum: int = 0) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key}：必须是整数")
    if value < minimum:
        raise ConfigError(f"{where}.{key}：必须 >= {minimum}，当前 {value}")
    return value


def _opt_float(section: dict[str, Any], key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key}：必须是数字")
    return float(value)


# ---------------------------------------------------------------- 各段解析


def _parse_app(raw: dict[str, Any]) -> AppConfig:
    section = _section(raw, "app", "config.yaml")
    where = "config.yaml.app"
    timezone = _opt_str(section, "timezone", "Asia/Shanghai", where)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{where}.timezone：无效时区 {timezone!r}") from None

    raw_db_path = os.environ.get(ENV_DB_PATH) or _req_str(section, "db_path", where)
    log_path = _req_str(section, "log_path", where)
    return AppConfig(
        host=_opt_str(section, "host", "127.0.0.1", where),
        port=_req_int(section, "port", where),
        timezone=timezone,
        db_path=_resolve_path(raw_db_path),
        log_path=_resolve_path(log_path),
    )


def _resolve_path(raw: str) -> Path:
    """相对路径一律相对项目根目录解析，避免受启动时 cwd 影响。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _parse_digest(raw: dict[str, Any]) -> DigestConfig:
    section = _section(raw, "digest", "config.yaml")
    where = "config.yaml.digest"
    return DigestConfig(
        limit=_req_int(section, "limit", where),
        lookback_hours=_req_int(section, "lookback_hours", where),
    )


def _parse_fetch(raw: dict[str, Any]) -> FetchConfig:
    section = _section(raw, "fetch", "config.yaml")
    where = "config.yaml.fetch"
    return FetchConfig(
        timeout_seconds=_req_int(section, "timeout_seconds", where),
        user_agent=_req_str(section, "user_agent", where),
        max_items_per_source=_req_int(section, "max_items_per_source", where),
        max_concurrency=_req_int(section, "max_concurrency", where),
    )


def _parse_scoring(raw: dict[str, Any]) -> ScoringConfig:
    section = _section(raw, "scoring", "config.yaml")
    where = "config.yaml.scoring"
    return ScoringConfig(
        concurrency=_req_int(section, "concurrency", where),
        max_summary_chars=_req_int(section, "max_summary_chars", where),
        max_attempts=_req_int(section, "max_attempts", where),
        prompt_version=_req_str(section, "prompt_version", where),
        temperature=_require_float(section, "temperature", where),
    )


def _parse_llm(raw: dict[str, Any]) -> LLMConfig:
    section = _section(raw, "llm", "config.yaml")
    where = "config.yaml.llm"
    api_key_env = _opt_str(section, "api_key_env", "DESKHUB_LLM_API_KEY", where)
    return LLMConfig(
        base_url=os.environ.get(ENV_LLM_BASE_URL) or _req_str(section, "base_url", where),
        model=os.environ.get(ENV_LLM_MODEL) or _req_str(section, "model", where),
        api_key_env=api_key_env,
        api_key=os.environ.get(api_key_env, ""),
        timeout_seconds=_req_int(section, "timeout_seconds", where),
    )


def _parse_modules(raw: dict[str, Any]) -> dict[str, ModuleConfig]:
    section = _section(raw, "modules", "config.yaml")
    modules: dict[str, ModuleConfig] = {}
    for key, value in section.items():
        if key not in VALID_MODULES:
            raise ConfigError(
                f"config.yaml.modules：未知 module {key!r}（允许：{', '.join(VALID_MODULES)}）"
            )
        if not isinstance(value, dict):
            raise ConfigError(f"config.yaml.modules.{key}：必须是对象")
        modules[key] = ModuleConfig(
            key=key,
            label=_req_str(value, "label", f"config.yaml.modules.{key}"),
            focus=_req_str(value, "focus", f"config.yaml.modules.{key}"),
        )
    missing = [key for key in VALID_MODULES if key not in modules]
    if missing:
        raise ConfigError(f"config.yaml.modules：缺少 {' / '.join(missing)} 的定义")
    return modules


def _parse_coins(raw: dict[str, Any]) -> CoinsConfig:
    section = _section(raw, "coins", "config.yaml")
    where = "config.yaml.coins"
    price = _section(section, "price", where)
    profile_llm = _section(section, "profile_llm", where)
    numista = _section(section, "numista", where)

    statistic = _opt_str(price, "statistic", "median", f"{where}.price")
    if statistic not in ("median", "mean"):
        raise ConfigError(f"{where}.price.statistic：只支持 median / mean，当前 {statistic!r}")

    collection = section.get("collection") or {}
    if not isinstance(collection, dict):
        raise ConfigError(f"{where}.collection：必须是对象")
    catalogue = collection.get("explore_catalogue") or {}
    if not isinstance(catalogue, dict):
        raise ConfigError(f"{where}.collection.explore_catalogue：必须是对象")
    raw_tiers = collection.get("denomination_tiers") or []
    if not isinstance(raw_tiers, list) or any(not isinstance(tier, list) for tier in raw_tiers):
        raise ConfigError(f"{where}.collection.denomination_tiers：必须是二维列表")
    tiers: list[tuple[str, ...]] = []
    for index, tier in enumerate(raw_tiers):
        values = tuple(str(value).strip() for value in tier if str(value).strip())
        if len(values) < 2:
            raise ConfigError(
                f"{where}.collection.denomination_tiers[{index}]：每组至少两个面值"
            )
        tiers.append(values)

    numista_key_env = _opt_str(numista, "api_key_env", "DESKHUB_NUMISTA_API_KEY", f"{where}.numista")

    return CoinsConfig(
        enabled=_opt_bool(section, "enabled", True, where),
        orders_xlsx=_resolve_path(_req_str(section, "orders_xlsx", where)),
        request_interval_seconds=_req_int(section, "request_interval_seconds", where, minimum=0),
        max_sessions_per_run=_req_int(section, "max_sessions_per_run", where),
        finalize_days=_req_int(section, "finalize_days", where, minimum=0),
        match_threshold=_require_float(section, "match_threshold", where),
        gapi_base_url=_opt_str(section, "gapi_base_url", "https://gapi.mxiqi.com", where),
        price=CoinPriceConfig(
            status=_opt_str(price, "status", "成交", f"{where}.price"),
            window_days=_req_int(price, "window_days", f"{where}.price"),
            min_samples=_req_int(price, "min_samples", f"{where}.price", minimum=1),
            statistic=statistic,
            query_interval_seconds=_req_int(
                price, "query_interval_seconds", f"{where}.price", minimum=0
            ),
            max_queries_per_run=_req_int(price, "max_queries_per_run", f"{where}.price"),
        ),
        profile_llm=CoinProfileLLMConfig(
            prompt_version=_opt_str(profile_llm, "prompt_version", "coins-v1", f"{where}.profile_llm"),
            batch_size=_req_int(profile_llm, "batch_size", f"{where}.profile_llm"),
            concurrency=_req_int(profile_llm, "concurrency", f"{where}.profile_llm"),
        ),
        numista=CoinNumistaConfig(
            base_url=_opt_str(numista, "base_url", "https://api.numista.com/v3", f"{where}.numista"),
            api_key_env=numista_key_env,
            api_key=os.environ.get(numista_key_env, ""),
            request_interval_seconds=_req_int(
                numista, "request_interval_seconds", f"{where}.numista", minimum=0
            ),
            max_series=_req_int(numista, "max_series", f"{where}.numista", minimum=1),
            timeout_seconds=_req_int(numista, "timeout_seconds", f"{where}.numista", minimum=1),
            monthly_limit=_req_int(numista, "monthly_limit", f"{where}.numista", minimum=1),
            max_requests_per_run=_req_int(
                numista, "max_requests_per_run", f"{where}.numista", minimum=1
            ),
            cdp_url=_opt_str(
                numista, "cdp_url", "http://127.0.0.1:9222", f"{where}.numista"
            ),
        ),
        collection=CoinCollectionConfig(
            min_results=_opt_int(collection, "min_results", 5, f"{where}.collection", minimum=1),
            rarity_floor=_opt_float(collection, "rarity_floor", 0.005, f"{where}.collection"),
            top_upgrades_per_series=_opt_int(
                collection, "top_upgrades_per_series", 3, f"{where}.collection", minimum=1
            ),
            denomination_tiers=tuple(tiers),
            explore_size_tolerance_mm=_opt_float(
                collection, "explore_size_tolerance_mm", 2.0, f"{where}.collection"
            ),
            explore_max_details=_opt_int(
                collection, "explore_max_details", 10, f"{where}.collection", minimum=1
            ),
            explore_catalogue=ExploreCatalogueConfig(
                collectible_type=_opt_int(
                    catalogue,
                    "collectible_type",
                    1,
                    f"{where}.collection.explore_catalogue",
                    minimum=1,
                ),
                year=_opt_str(
                    catalogue, "year", "1800-2026", f"{where}.collection.explore_catalogue"
                ),
                composition_id=_opt_int(
                    catalogue,
                    "composition_id",
                    5,
                    f"{where}.collection.explore_catalogue",
                    minimum=1,
                ),
                diameter_mm=_opt_str(
                    catalogue, "diameter_mm", "28-33", f"{where}.collection.explore_catalogue"
                ),
                per_page=_opt_int(
                    catalogue,
                    "per_page",
                    200,
                    f"{where}.collection.explore_catalogue",
                    minimum=1,
                ),
            ),
        ),
    )


def _parse_sources(raw: dict[str, Any], modules: dict[str, ModuleConfig]) -> tuple[SourceConfig, ...]:
    where = "sources.yaml"
    entries = raw.get("sources")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{where}：`sources` 必须是非空列表")

    sources: list[SourceConfig] = []
    seen: dict[str, str] = {}
    for index, entry in enumerate(entries):
        item_where = f"{where}.sources[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{item_where}：必须是对象")
        name = _req_str(entry, "name", item_where)
        url = _req_str(entry, "url", item_where)
        module = _req_str(entry, "module", item_where)
        if module not in modules:
            raise ConfigError(
                f"{item_where}.module：{module!r} 未在 config.yaml.modules 中定义"
            )
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{item_where}.url：必须是 http(s) 地址，当前 {url!r}")
        if url in seen:
            raise ConfigError(
                f"{where}：url 重复 —— {url!r} 同时出现在 {seen[url]} 与 {name!r}，"
                "同一源配两遍会导致重复抓取"
            )
        seen[url] = name
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{item_where}.enabled：必须是 true/false")
        published_tz = _parse_published_tz(entry, item_where)
        sources.append(
            SourceConfig(
                name=name, url=url, module=module, enabled=enabled, published_tz=published_tz
            )
        )
    return tuple(sources)


def _parse_published_tz(entry: dict[str, Any], where: str) -> str | None:
    """源可选声明「feed 时间戳所在时区」。

    用来兜住「把本地时间标成 GMT」的源：InfoQ 中文的 feed 就是这么写的，
    不声明的话它的 `published_at` 会整体偏 8 小时（见 plan §12 第 12 条）。
    """
    raw = entry.get("published_tz")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{where}.published_tz：必须是非空时区名，如 Asia/Shanghai")
    value = raw.strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{where}.published_tz：无效时区 {value!r}") from None
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件解析失败：{path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件内容必须是 YAML 对象：{path}")
    return data


# ---------------------------------------------------------------- 对外入口


def load(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    sources_path: Path | str = DEFAULT_SOURCES_PATH,
    *,
    env_file: Path | str | None = None,
) -> Config:
    """加载并校验配置，同时写入全局单例。失败抛 ConfigError。"""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    raw = _read_yaml(Path(config_path))
    app = _parse_app(raw)
    modules = _parse_modules(raw)
    sources = _parse_sources(_read_yaml(Path(sources_path)), modules)

    config = Config(
        app=app,
        digest=_parse_digest(raw),
        fetch=_parse_fetch(raw),
        scoring=_parse_scoring(raw),
        llm=_parse_llm(raw),
        modules=modules,
        sources=sources,
        coins=_parse_coins(raw),
    )

    global _config
    _config = config
    return config


def get() -> Config:
    """取全局配置；未加载时抛 ConfigError。"""
    if _config is None:
        raise ConfigError("配置尚未加载，请先调用 config.load()")
    return _config


def reset() -> None:
    """清空全局单例（测试用）。"""
    global _config
    _config = None


def warnings_for(config: Config) -> list[str]:
    """非致命问题，启动时打印警告即可。"""
    messages: list[str] = []
    if not config.llm.api_key:
        messages.append(
            f"未设置环境变量 {config.llm.api_key_env}，LLM 打分将不可用；"
            "抓取仍可正常运行（便于调试）"
        )
    if not config.coins.numista.api_key:
        messages.append(
            f"未设置环境变量 {config.coins.numista.api_key_env}，Numista 铜币参考库将不可用"
            "（其余功能不受影响）"
        )
    return messages
