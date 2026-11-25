"""主管路由、异常根因分级与 NL2SQL 自修复状态机离线单元测试。"""

from ecom_bi.anomaly import classify_root_cause
from ecom_bi.graph import route_and_extract


def test_supervisor_routing_and_entity_extraction() -> None:
    r1 = route_and_extract("请帮我删除 2026-09-01 的测试订单数据")
    assert r1["intent"] == "reject_unsafe"

    r2 = route_and_extract("请问净营收和 TACOS 的计算口径有什么区别？")
    assert r2["intent"] == "metric_qa"

    r3 = route_and_extract("为什么 2026-09-14 到 2026-09-16 饮水机净营收暴跌了？")
    assert r3["intent"] == "anomaly_investigation"
    assert r3["sku_id"] == "PET-FOUNTAIN-PRO"
    assert r3["start_date"] == "2026-09-14"
    assert r3["end_date"] == "2026-09-16"

    r4 = route_and_extract("查询 2026-09-01 到 2026-09-10 US 站点的净营收总额")
    assert r4["intent"] == "nl2sql"
    assert r4["marketplace"] == "US"


def test_root_cause_classifier_distinguishes_all_four_anomalies() -> None:
    # 1. 断数（广告正常但订单缺失）
    c1, _, conf1, _ = classify_root_cause(
        {"net_revenue_change_pct": -100.0},
        {"current_abnormal_rate_pct": 0.0, "baseline_abnormal_rate_pct": 6.0},
        {"missing_order_days": 2, "missing_dates": ["2026-09-19", "2026-09-20"], "ads_rows_on_missing_days": 6, "duplicate_order_rows": 0, "total_order_rows": 0},
    )
    assert c1 == "data_quality_pipeline_drop" and conf1 == "高"

    # 2. ETL 重复导入
    c2, _, _, _ = classify_root_cause(
        {"net_revenue_change_pct": 70.0},
        {"current_abnormal_rate_pct": 6.0, "baseline_abnormal_rate_pct": 6.0},
        {"missing_order_days": 0, "ads_rows_on_missing_days": 0, "duplicate_order_rows": 45, "total_order_rows": 100, "distinct_order_ids": 55},
    )
    assert c2 == "data_quality_duplicate_import"

    # 3. 品控退取消激增
    c3, _, _, _ = classify_root_cause(
        {"net_revenue_change_pct": -42.0},
        {"current_abnormal_rate_pct": 44.0, "baseline_abnormal_rate_pct": 6.5, "top_cancel_reason": "pump_noise_defect", "top_reason_count": 32},
        {"missing_order_days": 0, "ads_rows_on_missing_days": 0, "duplicate_order_rows": 0, "total_order_rows": 80},
    )
    assert c3 == "business_defect_cancel_refund_spike"

    # 4. 广告流量下滑与 ACOS 恶化
    c4, _, _, _ = classify_root_cause(
        {"net_revenue_change_pct": -58.0, "impressions_change_pct": -55.0, "current_acos_pct": 62.0, "baseline_acos_pct": 21.0},
        {"current_abnormal_rate_pct": 6.0, "baseline_abnormal_rate_pct": 6.0},
        {"missing_order_days": 0, "ads_rows_on_missing_days": 0, "duplicate_order_rows": 0, "total_order_rows": 25},
    )
    assert c4 == "business_ad_traffic_drop_acos_spike"


def test_bi_chinese_frontend_served() -> None:
    from pathlib import Path
    from ecom_bi.api import serve_frontend

    resp = serve_frontend()
    html = Path(resp.path).read_text(encoding="utf-8")
    assert "跨境电商 NL2SQL 智能问数与经营异常排查工作台" in html
    assert "V1 / V2 同屏对比" in html
