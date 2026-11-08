"""测试默认允许占位 API Key，避免离线装配依赖真实密钥。"""

import os

# 必须在导入 agent / interview_graph 的用例之前生效。
os.environ.setdefault("ALLOW_DUMMY_KEYS", "1")
