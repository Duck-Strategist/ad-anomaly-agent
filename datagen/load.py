"""把 datagen/out/*.csv 导入数据库（用管理员账号）。

用法：uv run python -m datagen.load
会先清空业务表再导入，可重复执行。审计表不清空。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "datagen" / "out"

# 按外键依赖顺序导入
TABLES = [
    "shops", "products", "orders", "order_items", "refunds",
    "ad_campaigns", "ad_groups", "ad_daily_stats", "fx_rates", "user_shop_permissions",
]


def admin_dsn() -> str:
    load_dotenv(ROOT / ".env")
    return (
        f"host={os.getenv('PG_HOST', 'localhost')} port={os.getenv('PG_PORT', '54329')} "
        f"dbname={os.getenv('PG_DB', 'adagent')} "
        f"user={os.getenv('PG_ADMIN_USER', 'app_admin')} password={os.getenv('PG_ADMIN_PASSWORD', 'admin_dev')}"
    )


def main() -> None:
    with psycopg.connect(admin_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE " + ", ".join(reversed(TABLES)) + " CASCADE")
            for t in TABLES:
                path = OUT_DIR / f"{t}.csv"
                with cur.copy(f"COPY {t} FROM STDIN WITH (FORMAT csv, NULL '')") as copy:
                    with open(path, "rb") as f:
                        while chunk := f.read(1 << 16):
                            copy.write(chunk)
                cur.execute(f"SELECT count(*) FROM {t}")
                print(f"{t:24s} {cur.fetchone()[0]:>8,}")
        conn.commit()


if __name__ == "__main__":
    main()
