"""项目二 FastAPI 服务入口（纯中文经营分析与异常排查工作台 + API）。"""

import time
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ecom_bi.anomaly import AnomalyInvestigator
from ecom_bi.config import get_settings
from ecom_bi.graph import EcomBIAgentService
from ecom_bi.observability import BIObservabilityRecorder
from ecom_bi.semantic_layer import get_semantic_layer
from ecom_bi.sql_guard import SafeSQLExecutor

_STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="跨境电商 NL2SQL 智能问数与经营异常排查平台",
    version="0.2.0",
)


class BIQueryRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=400)
    pipeline_version: Literal["v1_bare", "v2_semantic_agent"] = "v2_semantic_agent"


class BIFeedbackRequest(BaseModel):
    trace_id: str = Field(..., min_length=8)
    user_feedback: Literal[-1, 1]
    badcase_tag: Literal["wrong_metric", "sql_error", "wrong_root_cause", "slow_query", "other"] | None = None


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ecom_bi.dim_sku;")
        skus = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ecom_bi.fact_orders;")
        orders = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ecom_bi.fact_ads_daily;")
        ads = cur.fetchone()[0]
    return {
        "status": "ok",
        "schema": settings.bi_schema,
        "skus": skus,
        "orders": orders,
        "ads_daily": ads,
        "langfuse_url": settings.langfuse_host,
    }


@app.post("/api/v1/query")
def run_bi_query(req: BIQueryRequest) -> dict:
    t0 = time.perf_counter()
    settings = get_settings()
    semantic = get_semantic_layer()
    executor = SafeSQLExecutor(settings, semantic)
    investigator = AnomalyInvestigator(settings)
    agent = EcomBIAgentService(settings, semantic, executor, investigator)

    state = agent.ask(req.question, pipeline_version=req.pipeline_version)
    total_ms = int((time.perf_counter() - t0) * 1000)

    an_res = state.get("anomaly_result")
    recorder = BIObservabilityRecorder(settings)
    trace_id = recorder.record(
        pipeline_version=req.pipeline_version,
        intent=state.get("intent", "nl2sql"),
        question=req.question,
        answer=state.get("answer", ""),
        generated_sql=state.get("current_sql"),
        sql_retries=int(state.get("sql_retries", 0)),
        blocked_unsafe=bool(state.get("blocked_unsafe", False)),
        total_ms=total_ms,
        prompt_tokens=int(state.get("prompt_tokens", 0)),
        completion_tokens=int(state.get("completion_tokens", 0)),
        root_cause_code=an_res.get("root_cause_code") if an_res else None,
    )

    return {
        "trace_id": trace_id,
        "pipeline_version": req.pipeline_version,
        "intent": state.get("intent"),
        "blocked_unsafe": bool(state.get("blocked_unsafe", False)),
        "answer": state.get("answer", ""),
        "sql": state.get("current_sql"),
        "sql_retries": int(state.get("sql_retries", 0)),
        "sql_attempts": state.get("sql_attempts", []),
        "sql_columns": state.get("sql_columns", []),
        "sql_rows": state.get("sql_rows", []),
        "anomaly_result": an_res,
        "metrics": {
            "total_ms": total_ms,
            "sql_exec_ms": int(state.get("sql_exec_ms", 0)),
            "llm_ms": int(state.get("llm_ms", 0)),
            "prompt_tokens": int(state.get("prompt_tokens", 0)),
            "completion_tokens": int(state.get("completion_tokens", 0)),
        },
    }


@app.post("/api/v1/feedback")
def submit_feedback(req: BIFeedbackRequest) -> dict:
    settings = get_settings()
    recorder = BIObservabilityRecorder(settings)
    updated = recorder.record_feedback(req.trace_id, req.user_feedback, req.badcase_tag)
    if updated == 0:
        raise HTTPException(status_code=404, detail=f"未找到 trace_id={req.trace_id}")
    return {"status": "recorded", "trace_id": req.trace_id, "badcase_tag": req.badcase_tag}
