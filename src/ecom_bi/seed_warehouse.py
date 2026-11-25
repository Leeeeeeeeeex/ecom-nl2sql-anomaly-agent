"""初始化虚拟机 PostgreSQL ecom_bi 数仓并生成 90 天跨境宠物电商仿真数据（含 4 类精准注入异常）。

注入的 4 个可核验真实异常场景：
1. 2026-09-14 ~ 2026-09-16 | PET-FOUNTAIN-PRO | 真实业务波动（品控问题导致取消/退款率激增至 ~42%，净营收骤降）
2. 2026-09-19 ~ 2026-09-20 | PET-FEEDER-CAM   | 疑似数据管道断数（fact_orders 0 行缺失，但 fact_ads_daily 曝光点击正常）
3. 2026-09-22 ~ 2026-09-24 | PET-LITTER-AUTO  | 真实业务波动（广告曝光/点击腰斩、ACOS 飙升至 >55%，订单量下滑）
4. 2026-09-26              | US 站点全品       | 数据质量异常（ETL 重复导入导致部分 order_id 重复，GMV 虚高）
"""

import random
from datetime import date, timedelta

import psycopg

from ecom_bi.config import get_settings

SKUS = [
    ("PET-FOUNTAIN-PRO", "PurrFlow Pro 静音304不锈钢无线泵宠物饮水机 2.5L", "宠物饮水喂食", 45.99, 14.50, True),
    ("PET-FEEDER-CAM", "PurrFlow Vision 4L 高清夜视Wi-Fi自动喂食器", "宠物饮水喂食", 89.99, 28.00, True),
    ("PET-LITTER-AUTO", "PawNest OpenAir 65L 开放式防夹全自动智能猫砂盆", "宠物清洁", 299.00, 105.00, True),
    ("PET-CLIPPER-LOWNOISE", "PawNest QuietCut 低噪全身水洗宠物剃毛器套装", "宠物美容", 34.99, 10.20, True),
]
MARKETPLACES = ["US", "DE", "UK"]

DDL_SQL = """
CREATE SCHEMA IF NOT EXISTS ecom_bi;

DROP TABLE IF EXISTS ecom_bi.fact_orders;
DROP TABLE IF EXISTS ecom_bi.fact_ads_daily;
DROP TABLE IF EXISTS ecom_bi.dim_calendar;
DROP TABLE IF EXISTS ecom_bi.dim_sku;

CREATE TABLE ecom_bi.dim_sku (
    sku_id          TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL,
    unit_price_usd  NUMERIC(10, 2) NOT NULL,
    unit_cost_usd   NUMERIC(10, 2) NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE ecom_bi.dim_calendar (
    biz_date        DATE PRIMARY KEY,
    is_weekend      BOOLEAN NOT NULL,
    promo_event     TEXT NOT NULL DEFAULT 'None'
);

CREATE TABLE ecom_bi.fact_orders (
    row_id           BIGSERIAL PRIMARY KEY,
    order_id         TEXT NOT NULL,
    biz_date         DATE NOT NULL REFERENCES ecom_bi.dim_calendar(biz_date),
    marketplace      TEXT NOT NULL,
    sku_id           TEXT NOT NULL REFERENCES ecom_bi.dim_sku(sku_id),
    qty              INTEGER NOT NULL,
    gross_amount_usd NUMERIC(10, 2) NOT NULL,
    discount_usd     NUMERIC(10, 2) NOT NULL DEFAULT 0,
    refund_amount_usd NUMERIC(10, 2) NOT NULL DEFAULT 0,
    order_status     TEXT NOT NULL,
    cancel_reason    TEXT NOT NULL DEFAULT 'None'
);

CREATE INDEX idx_fact_orders_date_sku ON ecom_bi.fact_orders (biz_date, sku_id);
CREATE INDEX idx_fact_orders_mp_date ON ecom_bi.fact_orders (marketplace, biz_date);
CREATE INDEX idx_fact_orders_order_id ON ecom_bi.fact_orders (order_id);

CREATE TABLE ecom_bi.fact_ads_daily (
    biz_date        DATE NOT NULL REFERENCES ecom_bi.dim_calendar(biz_date),
    marketplace     TEXT NOT NULL,
    sku_id          TEXT NOT NULL REFERENCES ecom_bi.dim_sku(sku_id),
    impressions     INTEGER NOT NULL,
    clicks          INTEGER NOT NULL,
    ad_spend_usd    NUMERIC(10, 2) NOT NULL,
    ad_sales_usd    NUMERIC(10, 2) NOT NULL,
    PRIMARY KEY (biz_date, marketplace, sku_id)
);

CREATE TABLE IF NOT EXISTS ecom_bi.bi_audit_logs (
    id                  BIGSERIAL PRIMARY KEY,
    trace_id            TEXT NOT NULL,
    pipeline_version    TEXT NOT NULL,
    intent              TEXT NOT NULL,
    question            TEXT NOT NULL,
    generated_sql       TEXT,
    sql_retries         INTEGER NOT NULL DEFAULT 0,
    blocked_unsafe      BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    user_feedback       SMALLINT,
    badcase_tag         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def seed() -> dict:
    """确定性生成 90 天跨境电商数据并写入虚拟机 PostgreSQL ecom_bi Schema。"""
    rng = random.Random(20260928)
    start_day = date(2026, 7, 1)
    days = [start_day + timedelta(days=i) for i in range(90)]  # 2026-07-01 ~ 2026-09-28

    calendar_rows = []
    for d in days:
        promo = "None"
        if date(2026, 7, 15) <= d <= date(2026, 7, 16):
            promo = "PrimeDay"
        elif date(2026, 8, 20) <= d <= date(2026, 8, 22):
            promo = "BackToSchool"
        elif date(2026, 9, 5) <= d <= date(2026, 9, 7):
            promo = "AutumnSale"
        calendar_rows.append((d, d.weekday() >= 5, promo))

    base_daily_orders = {
        "PET-FOUNTAIN-PRO": {"US": 22, "DE": 9, "UK": 7},
        "PET-FEEDER-CAM": {"US": 14, "DE": 6, "UK": 5},
        "PET-LITTER-AUTO": {"US": 6, "DE": 3, "UK": 2},
        "PET-CLIPPER-LOWNOISE": {"US": 18, "DE": 8, "UK": 6},
    }
    price_map = {s[0]: s[3] for s in SKUS}

    order_rows = []
    ads_rows = []
    seq = 100000

    for d, is_wknd, promo in calendar_rows:
        promo_mult = 1.45 if promo != "None" else (1.12 if is_wknd else 1.0)
        for sku_id, _, _, price, _, _ in SKUS:
            for mp in MARKETPLACES:
                base_n = base_daily_orders[sku_id][mp]
                n_orders = max(1, int(round(base_n * promo_mult * rng.uniform(0.88, 1.12))))

                # 正常广告基线（ACOS 约 18%~24%）
                impressions = int(n_orders * rng.randint(180, 230))
                clicks = max(5, int(impressions * rng.uniform(0.028, 0.036)))
                ad_spend = round(clicks * rng.uniform(0.55, 0.85), 2)
                ad_sales = round(ad_spend / rng.uniform(0.19, 0.24), 2)

                # ---------- 异常场景 B：2026-09-19 ~ 2026-09-20 PET-FEEDER-CAM 订单管道断数 ----------
                is_pipeline_drop = (
                    sku_id == "PET-FEEDER-CAM" and date(2026, 9, 19) <= d <= date(2026, 9, 20)
                )

                # ---------- 异常场景 C：2026-09-22 ~ 2026-09-24 PET-LITTER-AUTO 广告流量腰斩 & ACOS 恶化 ----------
                if sku_id == "PET-LITTER-AUTO" and date(2026, 9, 22) <= d <= date(2026, 9, 24):
                    n_orders = max(1, int(round(n_orders * 0.35)))
                    impressions = int(impressions * 0.42)
                    clicks = max(2, int(clicks * 0.40))
                    ad_spend = round(ad_spend * 1.15, 2)
                    ad_sales = round(ad_spend / rng.uniform(0.58, 0.68), 2)  # ACOS ~62%

                ads_rows.append((d, mp, sku_id, impressions, clicks, ad_spend, ad_sales))

                if is_pipeline_drop:
                    # 订单表断数 0 行，但广告表正常，供跨表数据质量排查识别
                    continue

                # ---------- 异常场景 A：2026-09-14 ~ 2026-09-16 PET-FOUNTAIN-PRO 品控退取消激增 ----------
                is_defect_spike = (
                    sku_id == "PET-FOUNTAIN-PRO" and date(2026, 9, 14) <= d <= date(2026, 9, 16)
                )

                day_sku_orders = []
                for _ in range(n_orders):
                    seq += 1
                    oid = f"ORD-{d.strftime('%Y%m%d')}-{seq}"
                    qty = 1 if rng.random() < 0.88 else 2
                    gross = round(price_map[sku_id] * qty, 2)
                    disc = round(gross * (0.10 if promo != "None" else 0.02), 2)

                    r = rng.random()
                    if is_defect_spike:
                        if r < 0.24:
                            status, refund, reason = "cancelled", 0.0, "pump_noise_defect"
                        elif r < 0.46:
                            status, refund, reason = "refunded", round(gross - disc, 2), "pump_noise_defect"
                        else:
                            status, refund, reason = "completed", 0.0, "None"
                    else:
                        if r < 0.035:
                            status, refund, reason = "cancelled", 0.0, "buyer_remorse"
                        elif r < 0.030:
                            status, refund, reason = "refunded", round(gross - disc, 2), "shipping_delay"
                        else:
                            status, refund, reason = "completed", 0.0, "None"

                    row = (oid, d, mp, sku_id, qty, gross, disc, refund, status, reason)
                    day_sku_orders.append(row)

                order_rows.extend(day_sku_orders)

                # ---------- 异常场景 D：2026-09-26 US 站点 ETL 重复导入 ----------
                if d == date(2026, 9, 26) and mp == "US":
                    dup_count = max(1, int(len(day_sku_orders) * 0.75))
                    order_rows.extend(day_sku_orders[:dup_count])

    settings = get_settings()
    with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(DDL_SQL)
        cur.executemany(
            "INSERT INTO ecom_bi.dim_sku VALUES (%s, %s, %s, %s, %s, %s);",
            SKUS,
        )
        cur.executemany(
            "INSERT INTO ecom_bi.dim_calendar VALUES (%s, %s, %s);",
            calendar_rows,
        )
        cur.executemany(
            """
            INSERT INTO ecom_bi.fact_orders (
                order_id, biz_date, marketplace, sku_id, qty,
                gross_amount_usd, discount_usd, refund_amount_usd,
                order_status, cancel_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            order_rows,
        )
        cur.executemany(
            """
            INSERT INTO ecom_bi.fact_ads_daily (
                biz_date, marketplace, sku_id, impressions, clicks, ad_spend_usd, ad_sales_usd
            ) VALUES (%s, %s, %s, %s, %s, %s, %s);
            """,
            ads_rows,
        )
        conn.commit()

    return {
        "schema": "ecom_bi",
        "skus": len(SKUS),
        "calendar_days": len(calendar_rows),
        "orders": len(order_rows),
        "ads_daily": len(ads_rows),
    }


if __name__ == "__main__":
    summary = seed()
    print("=== 跨境宠物电商仿真数仓 (ecom_bi) 初始化完成 ===")
    print(summary)
