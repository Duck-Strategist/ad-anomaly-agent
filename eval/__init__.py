import os

# 评测默认不上报 Langfuse（SPEC Q24）；eval.run --trace 才上报，并自动打上 eval 标签
os.environ.setdefault("ADAGENT_TRACE", "0")
