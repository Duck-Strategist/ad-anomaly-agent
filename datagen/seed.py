"""容器启动时的一次性数据准备（SPEC Q23）：库里没有数据才生成并导入，有数据就跳过。

用法（compose 的 seed 服务）：python -m datagen.seed
跳过的原因：每次 up 都重新导入会冲掉演示状态（如已暂停的广告组、审批记录对应的广告组状态）。
要重置数据，在本机跑 uv run python -m datagen.load，或 docker compose down -v 后重新 up。
"""

from __future__ import annotations

import psycopg

from . import generate, load


def main() -> None:
    with psycopg.connect(load.admin_dsn()) as conn:
        n = conn.execute("SELECT count(*) FROM shops").fetchone()[0]
    if n:
        print(f"seed: database already has data ({n} shops), skipping")
        return
    print("seed: empty database, generating and loading synthetic data (fixed seed)")
    generate.main()
    load.main()


if __name__ == "__main__":
    main()
