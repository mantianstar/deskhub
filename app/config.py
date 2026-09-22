"""配置加载与校验。

全局唯一配置入口：启动时一次性加载，校验失败直接抛 ConfigError（由调用方退出进程）。

覆盖优先级：环境变量 > .env > config.yaml
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ModuleConfig:
    key: str
    label: str
    focus: str


@dataclass(frozen=True)
class SourceConfig:
    name: str
    url: str
    module: str
    enabled: bool


@dataclass(frozen=True)
class Config:
    app: AppConfig
    digest: DigestConfig
    fetch: FetchConfig
    scoring: ScoringConfig
    llm: LLMConfig
    modules: dict[str, ModuleConfig]
    sources: tuple[SourceConfig, ...]

    def module(self, key: str) -> ModuleConfig:
        try:
            return self.modules[key]
        except KeyError:
            raise ConfigError(f"未知 module：{key}（可选：{', '.join(self.modules)}）") from None


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
        sources.append(SourceConfig(name=name, url=url, module=module, enabled=enabled))
    return tuple(sources)


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
    return messages
