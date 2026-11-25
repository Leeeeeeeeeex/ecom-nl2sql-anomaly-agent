"""三路并发经营异常根因排查引擎（营收与广告 / 取消与退货 / 数据质量与断数重复检测）。"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import psycopg

from ecom_bi.config import Settings


@dataclass
class BranchFinding:
    """单路排查分支的结果与耗时记录。"""

    branch: str
    elapsed_ms: int
    metrics: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    sql_used: str = ""


@dataclass
class AnomalyInvestigationResult:
    """三路汇总与归因分级结论。"""

    start_date: str
    end_date: str
    sku_id: str | None
    marketplace: str | None
    execution_mode: Literal["parallel", "serial"]
    total_ms: int
    root_cause_code: str
    root_cause_label: str
    confidence: Literal["高", "中", "低"]
    summary_evidence: list[str]
    branches: list[BranchFinding]


def classify_root_cause(
    rev_metrics: dict[str, Any],
    cancel_metrics: dict[str, Any],
    dq_metrics: dict[str, Any],
) -> tuple[str, str, Literal["高", "中", "低"], list[str]]:
    """结合三路指标与时间窗口重叠情况，明确区分“数据管道问题”与“真实业务波动”。"""
    evidence: list[str] = []

    missing_days = int(dq_metrics.get("missing_order_days", 0) or 0)
    ads_active_on_missing = int(dq_metrics.get("ads_rows_on_missing_days", 0) or 0)
    dup_rows = int(dq_metrics.get("duplicate_order_rows", 0) or 0)
    total_rows = int(dq_metrics.get("total_order_rows", 0) or 0)

    # 1. 优先排查数据管道断数（避免把断数误报成业务暴跌）
    if missing_days > 0 and ads_active_on_missing > 0:
        evidence.append(
            f"目标区间内订单表存在 {missing_days} 天完全 0 行断数（缺失日期: {dq_metrics.get('missing_dates')}），"
            f"但同期广告表仍有 {ads_active_on_missing} 条正常曝光与投放记录。"
        )
        evidence.append("跨表交叉核验确认属于上游订单 ETL 同步中断导致的“假性营收暴跌”，并非真实需求归零。")
        return (
            "data_quality_pipeline_drop",
            "疑似数据管道故障：订单表断数（非真实业务暴跌）",
            "高",
            evidence,
        )

    # 2. 排查 ETL 重复导入导致的 GMV/订单数虚高
    dup_ratio = (dup_rows / total_rows) if total_rows > 0 else 0.0
    if dup_rows > 0 and dup_ratio >= 0.05:
        evidence.append(
            f"目标区间订单表共 {total_rows} 行，唯一 order_id 仅 {dq_metrics.get('distinct_order_ids')} 个，"
            f"检测到 {dup_rows} 行重复导入（重复率 {dup_ratio * 100:.1f}%）。"
        )
        evidence.append("重复行导致未去重 GMV 虚高，需排查 ETL 幂等键并按 order_id 去重重算。")
        return (
            "data_quality_duplicate_import",
            "数据质量异常：订单表 ETL 重复导入（GMV 虚高）",
            "高",
            evidence,
        )

    # 3. 排查真实业务波动：退货/取消激增
    cur_abn = float(cancel_metrics.get("current_abnormal_rate_pct", 0.0) or 0.0)
    base_abn = float(cancel_metrics.get("baseline_abnormal_rate_pct", 0.0) or 0.0)
    rev_chg = float(rev_metrics.get("net_revenue_change_pct", 0.0) or 0.0)
    if cur_abn - base_abn >= 15.0:
        evidence.append(
            f"订单表无断数与重复导入（数据完整），但异常退/取消率从基线 {base_abn:.1f}% 飙升至 {cur_abn:.1f}%（+{cur_abn - base_abn:.1f} pp）。"
        )
        evidence.append(
            f"主导退取消原因为 `{cancel_metrics.get('top_cancel_reason')}`（占比 {cancel_metrics.get('top_reason_count')} 单），"
            f"直接拖累日均净营收环比变化 {rev_chg:+.1f}%。"
        )
        return (
            "business_defect_cancel_refund_spike",
            "真实业务波动：品控/履约引发取消与退款激增",
            "高",
            evidence,
        )

    # 4. 排查真实业务波动：广告流量下滑与 ACOS 恶化
    imp_chg = float(rev_metrics.get("impressions_change_pct", 0.0) or 0.0)
    cur_acos = float(rev_metrics.get("current_acos_pct", 0.0) or 0.0)
    base_acos = float(rev_metrics.get("baseline_acos_pct", 0.0) or 0.0)
    if imp_chg <= -30.0 and (cur_acos - base_acos >= 15.0 or cur_acos >= 45.0):
        evidence.append(
            f"数据质量完整且退单率平稳（当前 {cur_abn:.1f}% vs 基线 {base_abn:.1f}%），"
            f"但日均广告曝光环比骤降 {imp_chg:.1f}%，点击同步下滑。"
        )
        evidence.append(
            f"广告 ACOS 从基线 {base_acos:.1f}% 恶化至 {cur_acos:.1f}%（+{cur_acos - base_acos:.1f} pp），"
            f"日均净营收环比变化 {rev_chg:+.1f}%，判定为流量与广告效率下滑导致的业务波动。"
        )
        return (
            "business_ad_traffic_drop_acos_spike",
            "真实业务波动：广告流量腰斩与 ACOS 效率恶化",
            "高",
            evidence,
        )

    # 5. 无显著异常或依据不足
    evidence.append(
        f"数据完整（缺数 0 天、重复 {dup_rows} 行），日均净营收环比 {rev_chg:+.1f}%，"
        f"退取消率 {cur_abn:.1f}%（基线 {base_abn:.1f}%），ACOS {cur_acos:.1f}%（基线 {base_acos:.1f}%）。"
    )
    return (
        "normal_or_insufficient_evidence",
        "未发现显著单点异常或依据不足（建议扩大时间区间/细化维度人工复核）",
        "中",
        evidence,
    )


class AnomalyInvestigator:
    """支持并行与串行双模式的三路异常排查器（用于量化并发加速收益）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _build_filters(self, sku_id: str | None, marketplace: str | None, alias: str = "") -> tuple[str, list[Any]]:
        prefix = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        if sku_id:
            clauses.append(f"{prefix}sku_id = %s")
            params.append(sku_id)
        if marketplace:
            clauses.append(f"{prefix}marketplace = %s")
            params.append(marketplace)
        sql_part = (" AND " + " AND ".join(clauses)) if clauses else ""
        return sql_part, params

    def _branch_revenue_and_ads(
        self, start: date, end: date, base_start: date, base_end: date, sku_id: str | None, marketplace: str | None
    ) -> BranchFinding:
        t0 = time.perf_counter()
        flt, fparams = self._build_filters(sku_id, marketplace)
        days_cur = (end - start).days + 1
        days_base = (base_end - base_start).days + 1

        sql_ord = f"""
            SELECT
                COALESCE(SUM(CASE WHEN biz_date BETWEEN %s AND %s AND order_status = 'completed'
                    THEN gross_amount_usd - discount_usd ELSE 0 END), 0) AS cur_net_rev,
                COALESCE(SUM(CASE WHEN biz_date BETWEEN %s AND %s AND order_status = 'completed'
                    THEN gross_amount_usd - discount_usd ELSE 0 END), 0) AS base_net_rev,
                COUNT(*) FILTER (WHERE biz_date BETWEEN %s AND %s AND order_status = 'completed') AS cur_orders,
                COUNT(*) FILTER (WHERE biz_date BETWEEN %s AND %s AND order_status = 'completed') AS base_orders
            FROM ecom_bi.fact_orders
            WHERE biz_date BETWEEN %s AND %s {flt};
        """
        sql_ads = f"""
            SELECT
                COALESCE(SUM(CASE WHEN biz_date BETWEEN %s AND %s THEN impressions ELSE 0 END), 0) AS cur_imp,
                COALESCE(SUM(CASE WHEN biz_date BETWEEN %s AND %s THEN impressions ELSE 0 END), 0) AS base_imp,
                COALESCE(SUM(CASE WHEN biz_date BETWEEN %s AND %s THEN ad_spend_usd ELSE 0 END), 0) AS cur_spend,
                COALESCE(SUM(CASE WHEN biz_date BETWEEN %s AND %s THEN ad_sales_usd ELSE 0 END), 0) AS cur_ad_sales,
                COALESCE(SUM(CASE WHEN biz_date BETWEEN %s AND %s THEN ad_spend_usd ELSE 0 END), 0) AS base_spend,
                COALESCE(SUM(CASE WHEN biz_date BETWEEN %s AND %s THEN ad_sales_usd ELSE 0 END), 0) AS base_ad_sales
            FROM ecom_bi.fact_ads_daily
            WHERE biz_date BETWEEN %s AND %s {flt};
        """

        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL default_transaction_read_only = ON;")
            cur.execute(sql_ord, [start, end, base_start, base_end, start, end, base_start, base_end, base_start, end, *fparams])
            cur_rev, base_rev, cur_ord, base_ord = cur.fetchone()
            cur.execute(
                sql_ads,
                [start, end, base_start, base_end, start, end, start, end, base_start, base_end, base_start, base_end, base_start, end, *fparams],
            )
            cur_imp, base_imp, cur_spend, cur_ad_sales, base_spend, base_ad_sales = cur.fetchone()

        cur_rev_day = float(cur_rev) / max(1, days_cur)
        base_rev_day = float(base_rev) / max(1, days_base)
        rev_chg = ((cur_rev_day - base_rev_day) / base_rev_day * 100.0) if base_rev_day > 0 else 0.0

        cur_imp_day = float(cur_imp) / max(1, days_cur)
        base_imp_day = float(base_imp) / max(1, days_base)
        imp_chg = ((cur_imp_day - base_imp_day) / base_imp_day * 100.0) if base_imp_day > 0 else 0.0

        cur_acos = (float(cur_spend) / float(cur_ad_sales) * 100.0) if float(cur_ad_sales) > 0 else 0.0
        base_acos = (float(base_spend) / float(base_ad_sales) * 100.0) if float(base_ad_sales) > 0 else 0.0

        flags = []
        if rev_chg <= -25.0:
            flags.append(f"日均净营收显著下滑 ({rev_chg:.1f}%)")
        if imp_chg <= -30.0:
            flags.append(f"日均广告曝光腰斩 ({imp_chg:.1f}%)")
        if cur_acos - base_acos >= 15.0:
            flags.append(f"ACOS 明显恶化 ({base_acos:.1f}% -> {cur_acos:.1f}%)")

        return BranchFinding(
            branch="revenue_and_ads",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            metrics={
                "current_daily_net_revenue_usd": round(cur_rev_day, 2),
                "baseline_daily_net_revenue_usd": round(base_rev_day, 2),
                "net_revenue_change_pct": round(rev_chg, 2),
                "current_completed_orders": int(cur_ord),
                "baseline_completed_orders": int(base_ord),
                "impressions_change_pct": round(imp_chg, 2),
                "current_acos_pct": round(cur_acos, 2),
                "baseline_acos_pct": round(base_acos, 2),
            },
            flags=flags,
            sql_used="fact_orders + fact_ads_daily 分表聚合环比检查",
        )

    def _branch_cancel_and_refund(
        self, start: date, end: date, base_start: date, base_end: date, sku_id: str | None, marketplace: str | None
    ) -> BranchFinding:
        t0 = time.perf_counter()
        flt, fparams = self._build_filters(sku_id, marketplace)
        sql = f"""
            SELECT
                COUNT(*) FILTER (WHERE biz_date BETWEEN %s AND %s) AS cur_tot,
                COUNT(*) FILTER (WHERE biz_date BETWEEN %s AND %s AND order_status IN ('cancelled', 'refunded')) AS cur_abn,
                COUNT(*) FILTER (WHERE biz_date BETWEEN %s AND %s AND order_status = 'cancelled') AS cur_can,
                COUNT(*) FILTER (WHERE biz_date BETWEEN %s AND %s AND order_status = 'refunded') AS cur_ref,
                COUNT(*) FILTER (WHERE biz_date BETWEEN %s AND %s) AS base_tot,
                COUNT(*) FILTER (WHERE biz_date BETWEEN %s AND %s AND order_status IN ('cancelled', 'refunded')) AS base_abn
            FROM ecom_bi.fact_orders
            WHERE biz_date BETWEEN %s AND %s {flt};
        """
        sql_reason = f"""
            SELECT cancel_reason, COUNT(*) AS cnt
            FROM ecom_bi.fact_orders
            WHERE biz_date BETWEEN %s AND %s
              AND order_status IN ('cancelled', 'refunded')
              {flt}
            GROUP BY cancel_reason
            ORDER BY cnt DESC
            LIMIT 3;
        """
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL default_transaction_read_only = ON;")
            cur.execute(sql, [start, end, start, end, start, end, start, end, base_start, base_end, base_start, base_end, base_start, end, *fparams])
            cur_tot, cur_abn, cur_can, cur_ref, base_tot, base_abn = cur.fetchone()
            cur.execute(sql_reason, [start, end, *fparams])
            reasons = cur.fetchall()

        cur_rate = (cur_abn / cur_tot * 100.0) if cur_tot else 0.0
        base_rate = (base_abn / base_tot * 100.0) if base_tot else 0.0
        top_reason = reasons[0][0] if reasons else "None"
        top_cnt = int(reasons[0][1]) if reasons else 0

        flags = []
        if cur_rate - base_rate >= 15.0:
            flags.append(f"异常退取消率飙升 ({base_rate:.1f}% -> {cur_rate:.1f}%，主因: {top_reason})")

        return BranchFinding(
            branch="cancel_and_refund",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            metrics={
                "current_total_orders": int(cur_tot),
                "current_cancelled": int(cur_can),
                "current_refunded": int(cur_ref),
                "current_abnormal_rate_pct": round(cur_rate, 2),
                "baseline_abnormal_rate_pct": round(base_rate, 2),
                "top_cancel_reason": top_reason,
                "top_reason_count": top_cnt,
            },
            flags=flags,
            sql_used="fact_orders 异常订单占比与 cancel_reason 归因分布",
        )

    def _branch_data_quality(
        self, start: date, end: date, sku_id: str | None, marketplace: str | None
    ) -> BranchFinding:
        t0 = time.perf_counter()
        flt_ord, fparams_ord = self._build_filters(sku_id, marketplace, alias="o")
        flt_plain, fparams_plain = self._build_filters(sku_id, marketplace)

        sql_missing = f"""
            SELECT c.biz_date,
                   COUNT(o.row_id) AS order_cnt
            FROM ecom_bi.dim_calendar c
            LEFT JOIN ecom_bi.fact_orders o
              ON c.biz_date = o.biz_date {flt_ord}
            WHERE c.biz_date BETWEEN %s AND %s
            GROUP BY c.biz_date
            ORDER BY c.biz_date;
        """
        sql_dup = f"""
            SELECT COUNT(*) AS total_rows,
                   COUNT(DISTINCT order_id) AS distinct_orders
            FROM ecom_bi.fact_orders
            WHERE biz_date BETWEEN %s AND %s {flt_plain};
        """

        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL default_transaction_read_only = ON;")
            cur.execute(sql_missing, [*fparams_ord, start, end])
            day_counts = cur.fetchall()
            missing_dates = [d.isoformat() for d, cnt in day_counts if cnt == 0]

            ads_on_missing = 0
            if missing_dates:
                sql_ads_check = f"""
                    SELECT COUNT(*)
                    FROM ecom_bi.fact_ads_daily
                    WHERE biz_date = ANY(%s::date[]) {flt_plain};
                """
                cur.execute(sql_ads_check, [missing_dates, *fparams_plain])
                ads_on_missing = int(cur.fetchone()[0])

            cur.execute(sql_dup, [start, end, *fparams_plain])
            total_rows, distinct_orders = cur.fetchone()

        dup_rows = int(total_rows) - int(distinct_orders)
        flags = []
        if missing_dates:
            flags.append(f"检测到 {len(missing_dates)} 天订单断数 ({', '.join(missing_dates)})")
        if dup_rows > 0:
            flags.append(f"检测到 {dup_rows} 行重复 order_id（ETL 重复导入）")

        return BranchFinding(
            branch="data_quality",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            metrics={
                "missing_order_days": len(missing_dates),
                "missing_dates": missing_dates,
                "ads_rows_on_missing_days": ads_on_missing,
                "total_order_rows": int(total_rows),
                "distinct_order_ids": int(distinct_orders),
                "duplicate_order_rows": dup_rows,
            },
            flags=flags,
            sql_used="dim_calendar 缺数扫描 + fact_ads_daily 跨表核验 + order_id 重复检测",
        )

    def investigate(
        self,
        start_date: str,
        end_date: str,
        sku_id: str | None = None,
        marketplace: str | None = None,
        mode: Literal["parallel", "serial"] = "parallel",
    ) -> AnomalyInvestigationResult:
        """执行三路异常排查（支持 parallel 与 serial 切换以实测并发性能收益）。"""
        s_date = date.fromisoformat(start_date)
        e_date = date.fromisoformat(end_date)
        base_end = s_date - timedelta(days=1)
        base_start = max(date(2026, 7, 1), base_end - timedelta(days=6))

        t0 = time.perf_counter()
        if mode == "parallel":
            with ThreadPoolExecutor(max_workers=3) as pool:
                f_rev = pool.submit(self._branch_revenue_and_ads, s_date, e_date, base_start, base_end, sku_id, marketplace)
                f_can = pool.submit(self._branch_cancel_and_refund, s_date, e_date, base_start, base_end, sku_id, marketplace)
                f_dq = pool.submit(self._branch_data_quality, s_date, e_date, sku_id, marketplace)
                b_rev, b_can, b_dq = f_rev.result(), f_can.result(), f_dq.result()
        else:
            b_rev = self._branch_revenue_and_ads(s_date, e_date, base_start, base_end, sku_id, marketplace)
            b_can = self._branch_cancel_and_refund(s_date, e_date, base_start, base_end, sku_id, marketplace)
            b_dq = self._branch_data_quality(s_date, e_date, sku_id, marketplace)

        total_ms = int((time.perf_counter() - t0) * 1000)
        code, label, conf, ev = classify_root_cause(b_rev.metrics, b_can.metrics, b_dq.metrics)
        return AnomalyInvestigationResult(
            start_date=start_date,
            end_date=end_date,
            sku_id=sku_id,
            marketplace=marketplace,
            execution_mode=mode,
            total_ms=total_ms,
            root_cause_code=code,
            root_cause_label=label,
            confidence=conf,
            summary_evidence=ev,
            branches=[b_rev, b_can, b_dq],
        )
