import os

# 测试不上报 Langfuse（SPEC Q24）
os.environ["ADAGENT_TRACE"] = "0"
