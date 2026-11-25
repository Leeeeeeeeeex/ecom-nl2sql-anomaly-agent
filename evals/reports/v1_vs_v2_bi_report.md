# 项目二评测报告：V1（裸 Schema + 串行排查）vs V2（语义层 + AST 护栏 + 自修复 + 并行排查）

- 评测时间：2026-11-25 17:52:57
- 数据源：虚拟机 PostgreSQL `ecom_bi`（90 天跨境宠物电商数仓，10,174 条订单 + 1,080 条广告记录 + 4 类注入异常）

## 核心量化指标对比

| 指标维度 | V1 裸 Schema 基线 | V2 语义层 + 并行 Agent | 提升幅度 |
| :--- | :---: | :---: | :---: |
| **NL2SQL 执行准确率 (EX)** | 50.0% (4/8) | **100.0% (8/8)** | +50.0 pp |
| **4 类经营异常根因命中率** | 100.0% (4/4) | **100.0% (4/4)** | — |
| **三路异常排查平均数据库耗时** | 29.8 ms（串行） | **12.0 ms（并行）** | **降延 59.7%** |
| **危险操作/SQL注入拦截率** | 100.0% | **100.0%** | 100% 拦截 |
| **指标口径问答与边界拒答率** | 100.0% | **100.0%** | 100% 准确 |
| **16 题总 Token（入/出）** | 6694 / 5209 | 18626 / 5508 | — |

## V1 翻车 vs V2 修复典型 Case（NL2SQL 口径与多表关联）

### BI02：计算 2026-09-14 到 2026-09-16 期间 PET-FOUNTAIN-PRO 的退货退款率（百分比，保留2位小数）。
- **标准答案结果**：`[{'refund_rate_pct': 37.08}]`
- **V1（裸 Schema）失败结果**：`[]`（错误：`查询执行成功但返回零行或全 NULL，请检查日期条件、SKU 编码或站点拼写。`）
  - V1 SQL：`SELECT ROUND(100.0 * COALESCE(SUM(f.refund_amount_usd), 0) / NULLIF(SUM(f.gross_amount_usd), 0), 2) AS refund_rate_pct FROM ecom_bi.fact_orders AS f JOIN ecom_bi.dim_sku AS s ON s.sku_id = f.sku_id WHERE f.biz_date BETWEEN CAST('2026-09-14' AS DATE) AND CAST('2026-09-16' AS DATE) AND s.title = 'PET-FOUNTAIN-PRO' LIMIT 200`
- **V2（语义层 + 自修复）通过结果**：`[{'refund_rate': 37.08}]`（重试次数：0）
  - V2 SQL：`SELECT ROUND(CAST(100.0 * COUNT(*) FILTER(WHERE order_status = 'refunded') / NULLIF(COUNT(*) FILTER(WHERE order_status IN ('completed', 'refunded')), 0) AS DECIMAL), 2) AS refund_rate FROM ecom_bi.fact_orders WHERE biz_date BETWEEN '2026-09-14' AND '2026-09-16' AND sku_id = 'PET-FOUNTAIN-PRO' LIMIT 200`

### BI03：计算 2026-09-01 到 2026-09-10 期间 PET-FOUNTAIN-PRO 的总广告销售成本比 TACOS（百分比，保留2位小数）。
- **标准答案结果**：`[{'tacos_pct': 10.03}]`
- **V1（裸 Schema）失败结果**：`[]`（错误：`查询执行成功但返回零行或全 NULL，请检查日期条件、SKU 编码或站点拼写。`）
  - V1 SQL：`SELECT ROUND(SUM(a.ad_spend_usd) * 100.0 / NULLIF(SUM(o.gross_amount_usd - o.discount_usd - o.refund_amount_usd), 0), 2) AS tacos_pct FROM ecom_bi.fact_ads_daily AS a JOIN ecom_bi.dim_sku AS s ON s.sku_id = a.sku_id JOIN ecom_bi.fact_orders AS o ON o.biz_date = a.biz_date AND o.marketplace = a.marketplace AND o.sku_id = a.sku_id WHERE s.title = 'PET-FOUNTAIN-PRO' AND a.biz_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-09-10' AS DATE) AND NOT o.order_status IN ('cancelled', 'canceled') LIMIT 200`
- **V2（语义层 + 自修复）通过结果**：`[{'tacos': 10.03}]`（重试次数：0）
  - V2 SQL：`SELECT ROUND(100.0 * SUM(a.ad_spend_usd) / NULLIF(SUM(o.net_revenue), 0), 2) AS tacos FROM (SELECT biz_date, sku_id, SUM(ad_spend_usd) AS ad_spend_usd FROM ecom_bi.fact_ads_daily WHERE biz_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-09-10' AS DATE) AND sku_id = 'PET-FOUNTAIN-PRO' GROUP BY biz_date, sku_id) AS a JOIN (SELECT biz_date, sku_id, SUM(CASE WHEN order_status = 'completed' THEN gross_amount_usd - discount_usd ELSE 0 END) AS net_revenue FROM ecom_bi.fact_orders WHERE biz_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-09-10' AS DATE) AND sku_id = 'PET-FOUNTAIN-PRO' GROUP BY biz_date, sku_id) AS o ON a.biz_date = o.biz_date AND a.sku_id = o.sku_id LIMIT 200`

### BI04：统计 2026-09-01 到 2026-09-10 期间各商品 SKU 的有效客单价 AOV（美元，保留2位小数），按 AOV 从高到低排序。
- **标准答案结果**：`[{'sku_id': 'PET-LITTER-AUTO', 'aov_usd': 308.32}, {'sku_id': 'PET-FEEDER-CAM', 'aov_usd': 95.98}, {'sku_id': 'PET-FOUNTAIN-PRO', 'aov_usd': 48.36}, {'sku_id': 'PET-CLIPPER-LOWNOISE', 'aov_usd': 36.79}]`
- **V1（裸 Schema）失败结果**：`[{'sku_id': 'PET-LITTER-AUTO', 'aov_usd': 284.24}, {'sku_id': 'PET-FEEDER-CAM', 'aov_usd': 85.42}, {'sku_id': 'PET-FOUNTAIN-PRO', 'aov_usd': 43.74}, {'sku_id': 'PET-CLIPPER-LOWNOISE', 'aov_usd': 33.26}]`（错误：`口径/多表关联算错`）
  - V1 SQL：`SELECT s.sku_id, ROUND(SUM(o.gross_amount_usd - o.discount_usd - o.refund_amount_usd) / NULLIF(SUM(o.qty), 0), 2) AS aov_usd FROM ecom_bi.fact_orders AS o JOIN ecom_bi.dim_sku AS s ON s.sku_id = o.sku_id WHERE o.biz_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-09-10' AS DATE) AND o.order_status = 'completed' GROUP BY s.sku_id ORDER BY aov_usd DESC LIMIT 200`
- **V2（语义层 + 自修复）通过结果**：`[{'sku_id': 'PET-LITTER-AUTO', 'aov': 308.32}, {'sku_id': 'PET-FEEDER-CAM', 'aov': 95.98}, {'sku_id': 'PET-FOUNTAIN-PRO', 'aov': 48.36}, {'sku_id': 'PET-CLIPPER-LOWNOISE', 'aov': 36.79}]`（重试次数：0）
  - V2 SQL：`SELECT s.sku_id, ROUND(SUM(o.gross_amount_usd - o.discount_usd) / NULLIF(COUNT(*), 0), 2) AS aov FROM ecom_bi.fact_orders AS o JOIN ecom_bi.dim_sku AS s ON s.sku_id = o.sku_id WHERE o.biz_date BETWEEN CAST('2026-09-01' AS DATE) AND CAST('2026-09-10' AS DATE) AND o.order_status = 'completed' GROUP BY s.sku_id ORDER BY aov DESC LIMIT 200`

### BI05：统计 2026-09-22 到 2026-09-24 期间 PET-LITTER-AUTO 的广告 ACOS（百分比，保留2位小数）。
- **标准答案结果**：`[{'acos_pct': 64.5}]`
- **V1（裸 Schema）失败结果**：`[]`（错误：`查询执行成功但返回零行或全 NULL，请检查日期条件、SKU 编码或站点拼写。`）
  - V1 SQL：`SELECT ROUND(100.0 * SUM(a.ad_spend_usd) / NULLIF(SUM(a.ad_sales_usd), 0), 2) AS acos_pct FROM ecom_bi.fact_ads_daily AS a JOIN ecom_bi.dim_sku AS s ON s.sku_id = a.sku_id WHERE a.biz_date BETWEEN CAST('2026-09-22' AS DATE) AND CAST('2026-09-24' AS DATE) AND s.title = 'PET-LITTER-AUTO' LIMIT 200`
- **V2（语义层 + 自修复）通过结果**：`[{'acos': 64.5}]`（重试次数：0）
  - V2 SQL：`SELECT ROUND(100.0 * SUM(ad_spend_usd) / NULLIF(SUM(ad_sales_usd), 0), 2) AS acos FROM ecom_bi.fact_ads_daily WHERE biz_date BETWEEN CAST('2026-09-22' AS DATE) AND CAST('2026-09-24' AS DATE) AND sku_id = 'PET-LITTER-AUTO' LIMIT 200`
