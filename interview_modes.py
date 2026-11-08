"""图、智能体和 HTTP 服务共用的面试模式词汇表。

本模块刻意不导入项目其余部分，也没有第三方依赖。因此静态文件服务可以
共用这份模式列表，而不必导入 LiveKit 或 LangChain；图也可以共用它，而不必
导入服务端。名字集中在一处，新模式就不会只加一半（例如 ``/api/token`` 接受、
图却拒绝）。
"""

from __future__ import annotations

MODE_NAMES: tuple[str, ...] = ("quick", "real")
DEFAULT_MODE = "quick"

MODE_LABELS: dict[str, str] = {
    "quick": "简单模式",
    "real": "实战模式",
}


def normalize_mode(value: str | None) -> str:
    """把任意用户或配置输入映射到已知的模式名。"""
    candidate = (value or "").strip().lower()
    return candidate if candidate in MODE_NAMES else DEFAULT_MODE


def is_valid_mode(value: str | None) -> bool:
    """仅当（去空白、忽略大小写后）是已知模式名时返回 True。"""
    return (value or "").strip().lower() in MODE_NAMES
