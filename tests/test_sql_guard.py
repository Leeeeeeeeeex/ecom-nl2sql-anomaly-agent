"""sqlglot AST SQL 安全护栏与语义层离线单元测试。"""

from ecom_bi.semantic_layer import get_semantic_layer
from ecom_bi.sql_guard import validate_and_rewrite_sql


def test_allows_valid_cte_select_and_injects_limit() -> None:
    semantic = get_semantic_layer()
    sql = """
    WITH ord AS (
        SELECT biz_date, SUM(gross_amount_usd - discount_usd) AS net_rev
        FROM ecom_bi.fact_orders
        WHERE order_status = 'completed'
        GROUP BY biz_date
    )
    SELECT biz_date, net_rev FROM ord ORDER BY biz_date
    """
    res = validate_and_rewrite_sql(sql, semantic.allowed_tables, max_rows=200)
    assert res.is_safe is True
    assert res.safe_sql is not None
    assert "LIMIT 200" in res.safe_sql
    assert "ecom_bi.fact_orders" in res.referenced_tables


def test_blocks_drop_delete_update_and_multi_statement_injection() -> None:
    semantic = get_semantic_layer()
    dangerous_queries = [
        "DROP TABLE ecom_bi.fact_orders",
        "DELETE FROM ecom_bi.fact_orders WHERE biz_date = '2026-09-01'",
        "UPDATE ecom_bi.dim_sku SET unit_price_usd = 1",
        "SELECT * FROM ecom_bi.dim_sku; DROP TABLE ecom_bi.dim_sku;",
        "WITH d AS (DELETE FROM ecom_bi.fact_orders RETURNING *) SELECT * FROM d",
    ]
    for q in dangerous_queries:
        res = validate_and_rewrite_sql(q, semantic.allowed_tables)
        assert res.is_safe is False, f"应拦截危险语句: {q}"


def test_blocks_non_whitelisted_tables_and_pg_sleep() -> None:
    semantic = get_semantic_layer()
    r1 = validate_and_rewrite_sql("SELECT * FROM pg_catalog.pg_shadow", semantic.allowed_tables)
    assert r1.is_safe is False
    assert "白名单" in (r1.reason or "")

    r2 = validate_and_rewrite_sql("SELECT * FROM public.review_chunks", semantic.allowed_tables)
    assert r2.is_safe is False

    r3 = validate_and_rewrite_sql("SELECT pg_sleep(10) FROM ecom_bi.dim_sku", semantic.allowed_tables)
    assert r3.is_safe is False
    assert "高危函数" in (r3.reason or "")


def test_semantic_layer_metric_lookup() -> None:
    semantic = get_semantic_layer()
    hits = semantic.lookup_metric_docs("请问净营收和 TACOS 在我们口径里怎么算？")
    keys = {h["metric_key"] for h in hits}
    assert "net_revenue" in keys
    assert "tacos" in keys
