"""selfheal/config.py — service.yaml 加载与 ${VAR} 环境变量替换。

确定性配置加载：把 config/service.yaml 解析为 {service_name: config_dict}，
字符串中的 ${VAR} 用传入 env 替换（默认 MOCK_PORT=18080，调用方可覆盖）。
"""

import os
import re
from pathlib import Path

import yaml

DEFAULT_ENV = {"MOCK_PORT": "18080"}
_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _resolve_str(text: str, env: dict) -> str:
    def _sub(m: re.Match) -> str:
        name = m.group(1)
        return env.get(name, m.group(0))  # 缺失时保留占位符，不静默清空

    return _VAR_RE.sub(_sub, text)


def _resolve_value(value, env: dict):
    """递归替换 dict/list/str 中的 ${VAR}。"""
    if isinstance(value, str):
        return _resolve_str(value, env)
    if isinstance(value, dict):
        return {k: _resolve_value(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, env) for v in value]
    return value


def load_services(yaml_path, env=None) -> dict:
    """加载 service.yaml。

    Args:
        yaml_path: config/service.yaml 路径
        env: 环境变量 dict，替换 ${VAR}。缺省 MOCK_PORT=18080。
    Returns:
        {service_name: config_dict}，字符串中的 ${VAR} 已替换。
    """
    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"service.yaml 不存在: {path}")
    env = {**DEFAULT_ENV, **dict(os.environ), **(env or {})}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "services" not in data:
        raise ValueError(f"service.yaml 缺少 services 根键: {path}")
    return {name: _resolve_value(cfg, env) for name, cfg in data["services"].items()}
