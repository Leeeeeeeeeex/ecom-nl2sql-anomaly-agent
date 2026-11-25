"""LangGraph 主管分流 + NL2SQL 报错/空结果自修复 + 三路并行异常排查状态机。"""

import json
import re
import time
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI

from ecom_bi.anomaly import AnomalyInvestigationResult, AnomalyInvestigator
from ecom_bi.config import Settings
from ecom_bi.semantic_layer import SemanticLayer
from ecom_bi.sql_guard import SafeSQLExecutor

_UNSAFE_INTENT_KEYWORDS = (
    "drop table",
    "delete from",
    "truncate",
    "update ",
    "insert into",
    "alter table",
    "pg_shadow",
    "pg_sleep",
    "删表",
    "删除",
    "删掉",
    "清空表",
    "修改价格",
    "篡改",
    "导出密码",
)
_KNOWN_SKUS = ("PET-FOUNTAIN-PRO", "PET-FEEDER-CAM", "PET-LITTER-AUTO", "PET-CLIPPER-LOWNOISE")
_SKU_ALIASES = {
    "饮水机": "PET-FOUNTAIN-PRO",
    "喂食器": "PET-FEEDER-CAM",
    "猫砂盆": "PET-LITTER-AUTO",
    "剃毛器": "PET-CLIPPER-LOWNOISE",
}
_DATE_RE = re.compile(r"2026-\d{2}-\d{2}")


class BIState(TypedDict, total=False):
    """LangGraph 全局状态定义。"""

    question: str
    pipeline_version: Literal["v1_bare", "v2_semantic_agent"]

    # 路由与提取范围
    intent: Literal["reject_unsafe", "metric_qa", "nl2sql", "anomaly_investigation"]
    start_date: str | None
    end_date: str | None
    sku_id: str | None
    marketplace: str | None

    # NL2SQL 自修复中间态
    current_sql: str | None
    sql_attempts: list[dict[str, Any]]
    sql_retries: int
    last_sql_error: str | None
    sql_rows: list[dict[str, Any]]
    sql_columns: list[str]
    sql_exec_ms: int

    # 异常排查中间态
    anomaly_result: dict[str, Any] | None

    # 输出与审计
    answer: str
    blocked_unsafe: bool
    prompt_tokens: int
    completion_tokens: int
    llm_ms: int


def route_and_extract(question: str) -> dict[str, Any]:
    """基于高确定性规则的主管路由与范围提取（零 LLM 延迟，避免误路由）。"""
    q_low = question.lower()
    if any(kw in q_low for kw in _UNSAFE_INTENT_KEYWORDS):
        return {"intent": "reject_unsafe"}

    # 提取 SKU / 站点 / 日期区间
    sku_id = None
    for code in _KNOWN_SKUS:
        if code.lower() in q_low:
            sku_id = code
            break
    if not sku_id:
        for alias, code in _SKU_ALIASES.items():
            if alias in question:
                sku_id = code
                break

    marketplace = None
    for mp in ("US", "DE", "UK"):
        if re.search(rf"\b{mp}\b", question, re.IGNORECASE) or (mp == "US" and "美国" in question) or (mp == "DE" and "德国" in question) or (mp == "UK" and "英国" in question):
            marketplace = mp
            break

    dates = _DATE_RE.findall(question)
    start_date = dates[0] if len(dates) >= 1 else None
    end_date = dates[1] if len(dates) >= 2 else start_date

    # 意图判定优先级：异常排查 > 口径定义问答 > 精准问数 NL2SQL
    anomaly_kws = ("为什么", "暴跌", "骤降", "飙升", "虚高", "异常", "排查", "根因", "断数", "翻倍", "掉单")
    metric_kws = ("口径", "怎么算", "定义", "公式", "区别", "什么意思")

    if any(kw in question for kw in anomaly_kws):
        return {
            "intent": "anomaly_investigation",
            "sku_id": sku_id,
            "marketplace": marketplace,
            "start_date": start_date or "2026-09-14",
            "end_date": end_date or start_date or "2026-09-16",
        }

    # 若纯问口径且无具体日期/榜单要求，路由到语义层口径问答
    if any(kw in question for kw in metric_kws) and not dates and not any(w in question for w in ("多少", "排名", "列出", "查询", "统计", "前")):
        return {"intent": "metric_qa", "sku_id": sku_id, "marketplace": marketplace}

    return {
        "intent": "nl2sql",
        "sku_id": sku_id,
        "marketplace": marketplace,
        "start_date": start_date,
        "end_date": end_date,
    }


class EcomBIAgentService:
    """跨境电商 NL2SQL 与经营异常排查 LangGraph 编排服务。"""

    def __init__(
        self,
        settings: Settings,
        semantic: SemanticLayer,
        sql_executor: SafeSQLExecutor,
        investigator: AnomalyInvestigator,
        llm_client: OpenAI | None = None,
    ) -> None:
        self._settings = settings
        self._semantic = semantic
        self._sql_executor = sql_executor
        self._investigator = investigator
        self._llm = llm_client
        self._graph = self._compile_graph()

    def _get_llm(self) -> OpenAI:
        if self._llm is not None:
            return self._llm
        if not self._settings.llm_api_key:
            raise ValueError("请先配置 DEEPSEEK_API_KEY 或 LLM_API_KEY。")
        self._llm = OpenAI(api_key=self._settings.llm_api_key, base_url=self._settings.llm_base_url)
        return self._llm

    def _call_llm(self, system: str, user: str) -> tuple[str, int, int, int]:
        t0 = time.perf_counter()
        resp = self._get_llm().chat.completions.create(
            model=self._settings.llm_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        return (
            content,
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
            int((time.perf_counter() - t0) * 1000),
        )

    def _node_route(self, state: BIState) -> BIState:
        info = route_and_extract(state["question"])
        return {
            "intent": info["intent"],
            "sku_id": info.get("sku_id"),
            "marketplace": info.get("marketplace"),
            "start_date": info.get("start_date"),
            "end_date": info.get("end_date"),
            "sql_attempts": [],
            "sql_retries": 0,
            "blocked_unsafe": info["intent"] == "reject_unsafe",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "llm_ms": 0,
        }

    def _route_branch(self, state: BIState) -> str:
        return state["intent"]

    def _node_reject_unsafe(self, state: BIState) -> BIState:
        return {
            "blocked_unsafe": True,
            "answer": "【安全护栏拦截】本系统仅支持只读经营分析查询，已拒绝包含删除、修改、跨库或高危系统函数在内的非只读请求。",
        }

    def _node_metric_qa(self, state: BIState) -> BIState:
        hits = self._semantic.lookup_metric_docs(state["question"])
        if not hits:
            return {
                "answer": "【口径未覆盖】当前语义层指标手册（semantic/metrics.yaml）暂未收录该指标定义，无法确认，建议联系数据分析团队补充口径。",
            }
        blocks = []
        for h in hits:
            part = (
                f"- **{h['display_name']} (`{h['metric_key']}`)**\n"
                f"  - 标准公式：`{h['formula']}`\n"
                f"  - 口径范围：{h['scope']}"
            )
            if h.get("pitfall"):
                part += f"\n  - 避坑提示：{h['pitfall']}"
            blocks.append(part)
        return {
            "answer": "根据语义层权威口径文档（`semantic/metrics.yaml`），相关指标定义如下：\n\n" + "\n".join(blocks),
        }

    def _node_nl2sql_generate(self, state: BIState) -> BIState:
        version = state.get("pipeline_version", "v2_semantic_agent")
        schema_ctx = (
            self._semantic.render_bare_schema_v1()
            if version == "v1_bare"
            else self._semantic.render_full_semantic_prompt_v2()
        )
        system = (
            "你是资深 PostgreSQL 数据分析师。请根据下方表结构与口径，输出一条合法的只读 SELECT SQL。"
            "只输出纯 SQL 语句本身，不要任何 Markdown 代码块、解释或多余文字。"
        )
        user_msg = f"{schema_ctx}\n\n【用户问题】\n{state['question']}"
        if state.get("last_sql_error"):
            user_msg += (
                f"\n\n【上一次 SQL 失败或返回空集，请修正】\n"
                f"上次 SQL：{state.get('current_sql')}\n"
                f"诊断反馈：{state.get('last_sql_error')}\n"
                f"请检查表名、日期区间（数据范围 2026-07-01 ~ 2026-09-28）、SKU 大小写或聚合逻辑后重新生成只读 SELECT SQL。"
            )

        sql_text, p_tok, c_tok, ms = self._call_llm(system, user_msg)
        cleaned = sql_text.removeprefix("```sql").removeprefix("```").removesuffix("```").strip()
        return {
            "current_sql": cleaned,
            "prompt_tokens": state.get("prompt_tokens", 0) + p_tok,
            "completion_tokens": state.get("completion_tokens", 0) + c_tok,
            "llm_ms": state.get("llm_ms", 0) + ms,
        }

    def _node_nl2sql_execute(self, state: BIState) -> BIState:
        raw_sql = state.get("current_sql") or ""
        attempts = list(state.get("sql_attempts", []))
        validation, exec_res = self._sql_executor.execute_checked(raw_sql)

        if not validation.is_safe:
            err = f"AST 安全护栏拒绝：{validation.reason}"
            attempts.append({"attempt": len(attempts) + 1, "sql": raw_sql, "status": "blocked", "error": err})
            return {
                "sql_attempts": attempts,
                "sql_retries": state.get("sql_retries", 0) + 1,
                "last_sql_error": err,
                "blocked_unsafe": True,
                "sql_rows": [],
                "sql_columns": [],
                "sql_exec_ms": 0,
            }

        assert exec_res is not None
        if not exec_res.ok:
            attempts.append({"attempt": len(attempts) + 1, "sql": exec_res.sql, "status": "db_error", "error": exec_res.error})
            return {
                "current_sql": exec_res.sql,
                "sql_attempts": attempts,
                "sql_retries": state.get("sql_retries", 0) + 1,
                "last_sql_error": f"数据库执行报错：{exec_res.error}",
                "sql_rows": [],
                "sql_columns": [],
                "sql_exec_ms": exec_res.exec_ms,
            }

        if exec_res.is_empty_or_all_null:
            err = "查询执行成功但返回零行或全 NULL，请检查日期条件、SKU 编码或站点拼写。"
            attempts.append({"attempt": len(attempts) + 1, "sql": exec_res.sql, "status": "empty_result", "error": err})
            return {
                "current_sql": exec_res.sql,
                "sql_attempts": attempts,
                "sql_retries": state.get("sql_retries", 0) + 1,
                "last_sql_error": err,
                "sql_rows": [],
                "sql_columns": exec_res.columns,
                "sql_exec_ms": exec_res.exec_ms,
            }

        attempts.append({"attempt": len(attempts) + 1, "sql": exec_res.sql, "status": "ok", "row_count": exec_res.row_count})
        return {
            "current_sql": exec_res.sql,
            "sql_attempts": attempts,
            "last_sql_error": None,
            "blocked_unsafe": False,
            "sql_rows": exec_res.rows,
            "sql_columns": exec_res.columns,
            "sql_exec_ms": exec_res.exec_ms,
        }

    def _route_after_sql_exec(self, state: BIState) -> Literal["retry", "summarize"]:
        version = state.get("pipeline_version", "v2_semantic_agent")
        if not state.get("last_sql_error"):
            return "summarize"
        # V1 基线不做报错/空结果自修复，直接结束
        if version == "v1_bare":
            return "summarize"
        if state.get("sql_retries", 0) <= self._settings.sql_max_retries:
            return "retry"
        return "summarize"

    def _node_nl2sql_summarize(self, state: BIState) -> BIState:
        if state.get("last_sql_error"):
            return {
                "answer": f"【问数未得到有效结果】已尝试 {len(state.get('sql_attempts', []))} 次查询，最后诊断：{state['last_sql_error']}。请核对查询日期（数仓覆盖 2026-07-01 ~ 2026-09-28）或商品编码后重试。",
            }
        rows = state.get("sql_rows", [])
        preview = json.dumps(rows[:15], ensure_ascii=False)
        system = (
            "你是严谨的电商数据分析师。请严格根据给定 SQL 和查询结果用简体中文简明作答，"
            "必须保留具体数字、时间区间与统计口径说明，严禁编造结果中不存在的数字。"
        )
        user = f"【用户问题】{state['question']}\n【执行 SQL】{state.get('current_sql')}\n【查询结果（共 {len(rows)} 行，展示前 15 行）】\n{preview}"
        ans, p_tok, c_tok, ms = self._call_llm(system, user)
        return {
            "answer": ans,
            "prompt_tokens": state.get("prompt_tokens", 0) + p_tok,
            "completion_tokens": state.get("completion_tokens", 0) + c_tok,
            "llm_ms": state.get("llm_ms", 0) + ms,
        }

    def _node_anomaly(self, state: BIState) -> BIState:
        version = state.get("pipeline_version", "v2_semantic_agent")
        mode: Literal["parallel", "serial"] = "serial" if version == "v1_bare" else "parallel"
        res: AnomalyInvestigationResult = self._investigator.investigate(
            start_date=state.get("start_date") or "2026-09-14",
            end_date=state.get("end_date") or "2026-09-16",
            sku_id=state.get("sku_id"),
            marketplace=state.get("marketplace"),
            mode=mode,
        )
        res_dict = {
            "start_date": res.start_date,
            "end_date": res.end_date,
            "sku_id": res.sku_id,
            "marketplace": res.marketplace,
            "execution_mode": res.execution_mode,
            "total_ms": res.total_ms,
            "root_cause_code": res.root_cause_code,
            "root_cause_label": res.root_cause_label,
            "confidence": res.confidence,
            "summary_evidence": res.summary_evidence,
            "branches": [
                {
                    "branch": b.branch,
                    "elapsed_ms": b.elapsed_ms,
                    "metrics": b.metrics,
                    "flags": b.flags,
                    "sql_used": b.sql_used,
                }
                for b in res.branches
            ],
        }

        system = (
            "你是资深跨境电商经营分析专家。请严格基于下方三路异常排查的真实统计证据，输出结构化排查结论："
            "1) 根因定性（明确区分数据管道问题还是真实业务波动）及可信度；"
            "2) 关键量化证据（保留具体日期、百分比、订单数、ACOS/退单率/缺数天数）；"
            "3) 下一步可执行止损或排查动作。严禁编造证据中未出现的数字。"
        )
        user = f"【用户问题】{state['question']}\n【三路排查结构化证据】\n{json.dumps(res_dict, ensure_ascii=False, indent=2)}"
        ans, p_tok, c_tok, ms = self._call_llm(system, user)
        return {
            "anomaly_result": res_dict,
            "answer": ans,
            "prompt_tokens": state.get("prompt_tokens", 0) + p_tok,
            "completion_tokens": state.get("completion_tokens", 0) + c_tok,
            "llm_ms": state.get("llm_ms", 0) + ms,
        }

    def _compile_graph(self):
        builder = StateGraph(BIState)
        builder.add_node("route", self._node_route)
        builder.add_node("reject_unsafe", self._node_reject_unsafe)
        builder.add_node("metric_qa", self._node_metric_qa)
        builder.add_node("nl2sql_generate", self._node_nl2sql_generate)
        builder.add_node("nl2sql_execute", self._node_nl2sql_execute)
        builder.add_node("nl2sql_summarize", self._node_nl2sql_summarize)
        builder.add_node("anomaly_investigation", self._node_anomaly)

        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route",
            self._route_branch,
            {
                "reject_unsafe": "reject_unsafe",
                "metric_qa": "metric_qa",
                "nl2sql": "nl2sql_generate",
                "anomaly_investigation": "anomaly_investigation",
            },
        )
        builder.add_edge("reject_unsafe", END)
        builder.add_edge("metric_qa", END)
        builder.add_edge("nl2sql_generate", "nl2sql_execute")
        builder.add_conditional_edges(
            "nl2sql_execute",
            self._route_after_sql_exec,
            {"retry": "nl2sql_generate", "summarize": "nl2sql_summarize"},
        )
        builder.add_edge("nl2sql_summarize", END)
        builder.add_edge("anomaly_investigation", END)
        return builder.compile()

    def ask(
        self,
        question: str,
        pipeline_version: Literal["v1_bare", "v2_semantic_agent"] = "v2_semantic_agent",
    ) -> BIState:
        return self._graph.invoke({"question": question, "pipeline_version": pipeline_version})
