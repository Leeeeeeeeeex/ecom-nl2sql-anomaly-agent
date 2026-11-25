"""跨境电商语义层加载器（统一管理指标口径、表结构、防坑约束与口径检索）。"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_YAML = Path(__file__).resolve().parents[2] / "semantic" / "metrics.yaml"


class SemanticLayer:
    """结构化语义层：为 V2 NL2SQL 与口径问答提供唯一权威真相源。"""

    def __init__(self, path: Path = _DEFAULT_YAML) -> None:
        self._path = path
        self._data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    @property
    def schema(self) -> str:
        return str(self._data.get("schema", "ecom_bi"))

    @property
    def allowed_tables(self) -> set[str]:
        return {str(t).lower() for t in self._data.get("allowed_tables", [])}

    @property
    def metrics(self) -> dict[str, dict[str, Any]]:
        return dict(self._data.get("metrics", {}))

    @property
    def tables(self) -> dict[str, dict[str, Any]]:
        return dict(self._data.get("tables", {}))

    def render_bare_schema_v1(self) -> str:
        """V1 基线上下文：仅提供裸表名与字段名（无业务口径公式与防坑约束）。"""
        lines = [f"PostgreSQL Schema: {self.schema}"]
        for tname, info in self.tables.items():
            cols = ", ".join(info.get("columns", {}).keys())
            lines.append(f"- {self.schema}.{tname}({cols})")
        return "\n".join(lines)

    def render_full_semantic_prompt_v2(self) -> str:
        """V2 生产上下文：完整表结构 + 枚举说明 + 权威指标公式 + 多表关联防坑守则。"""
        lines = [
            f"【业务域】{self._data.get('domain')}（PostgreSQL Schema: {self.schema}，查询时表名可写 {self.schema}.xxx）",
            "",
            "【数据表与字段说明】",
        ]
        for tname, info in self.tables.items():
            lines.append(f"- {self.schema}.{tname}：{info.get('description', '')}")
            for cname, cdesc in info.get("columns", {}).items():
                lines.append(f"    - {cname}: {cdesc}")

        lines.append("")
        lines.append("【权威业务指标口径（严禁擅自更改计算逻辑）】")
        for mkey, minfo in self.metrics.items():
            lines.append(f"- {minfo.get('display_name')} ({mkey})")
            lines.append(f"    - 标准公式: {minfo.get('formula')}")
            lines.append(f"    - 适用范围: {minfo.get('scope')}")
            if minfo.get("pitfall"):
                lines.append(f"    - 避坑约束: {minfo.get('pitfall')}")

        lines.extend(
            [
                "",
                "【SQL 编写强约束】",
                "1. 数据覆盖日期为 2026-07-01 至 2026-09-28，查询时必须显式限定 biz_date 区间。",
                "2. 若同一问题同时涉及 fact_orders 与 fact_ads_daily，必须先用 CTE 分别按相同粒度聚合后再 JOIN，严禁直接多对多 JOIN！",
                "3. 所有除法必须用 NULLIF(分母, 0) 防止除零；结果金额/百分比统一 ROUND(..., 2)。",
                "4. 若问题涉及去重订单统计或怀疑重复导入，可用 COUNT(DISTINCT order_id) 或检查 GROUP BY order_id HAVING COUNT(*) > 1。",
            ]
        )
        return "\n".join(lines)

    def lookup_metric_docs(self, question: str) -> list[dict[str, Any]]:
        """按问题关键词召回匹配的指标口径文档（用于 metric_qa 节点带出处回答）。"""
        q = question.lower()
        hits: list[dict[str, Any]] = []
        keyword_map = {
            "gmv": ["gmv", "毛成交", "毛额", "成交额"],
            "net_revenue": ["净营收", "净销售", "net revenue", "有效营收", "实收"],
            "cancel_rate": ["取消率", "cancel"],
            "refund_rate": ["退货率", "退款率", "refund"],
            "cancel_and_refund_rate": ["异常单", "退取消"],
            "acos": ["acos", "广告销售成本", "广告效率"],
            "tacos": ["tacos", "总广告"],
            "aov": ["aov", "客单价"],
        }
        for key, info in self.metrics.items():
            kws = keyword_map.get(key, [key])
            if any(kw in q for kw in kws):
                hits.append({"metric_key": key, **info})
        return hits


@lru_cache(maxsize=1)
def get_semantic_layer() -> SemanticLayer:
    return SemanticLayer()
