"""合作项目风险登记服务端。

模块划分：

- ``clock``：可控时钟，定时复查依赖注入时钟保证确定性。
- ``events``：事件类型常量。
- ``rules``：有版本的评级规则与评级引擎。
- ``store``：SQLite 持久化，事件哈希链 + 读模型投影。
- ``service``：应用服务，负责评级、传播、合并、降级、例外、复开等决定链。
- ``http_app``：基于标准库的 JSON HTTP 接口。
"""
from .clock import Clock, FixedClock, MutableClock, SystemClock
from .service import RiskService, ServiceError
from .store import Store
from .rules import RuleSet, evaluate

__all__ = [
    "Clock",
    "FixedClock",
    "MutableClock",
    "SystemClock",
    "RiskService",
    "ServiceError",
    "Store",
    "RuleSet",
    "evaluate",
]
