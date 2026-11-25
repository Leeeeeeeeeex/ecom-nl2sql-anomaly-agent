"""项目二可观测与审计模块（PostgreSQL bi_audit_logs + Langfuse v2 HTTP Ingestion）。"""

import base64
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import psycopg

from ecom_bi.config import Settings


class BIObservabilityRecorder:
    """记录 NL2SQL 与异常排查全链路耗时、SQL 重试次数、安全拦截状态与 Langfuse 追踪。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _langfuse_ready(self) -> bool:
        s = self._settings
        return bool(
            s.langfuse_enabled
            and s.langfuse_public_key
            and s.langfuse_secret_key
            and "xxxx" not in s.langfuse_public_key
            and "xxxx" not in s.langfuse_secret_key
        )

    def _post_langfuse(self, batch: list[dict]) -> None:
        token = base64.b64encode(
            f"{self._settings.langfuse_public_key}:{self._settings.langfuse_secret_key}".encode("utf-8")
        ).decode("ascii")
        url = f"{self._settings.langfuse_host.rstrip('/')}/api/public/ingestion"
        httpx.post(
            url,
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
            json={"batch": batch},
            timeout=5.0,
        )

    def record(
        self,
        pipeline_version: str,
        intent: str,
        question: str,
        answer: str,
        generated_sql: str | None,
        sql_retries: int,
        blocked_unsafe: bool,
        total_ms: int,
        prompt_tokens: int,
        completion_tokens: int,
        root_cause_code: str | None = None,
    ) -> str:
        trace_id = uuid.uuid4().hex
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ecom_bi.bi_audit_logs (
                    trace_id, pipeline_version, intent, question,
                    generated_sql, sql_retries, blocked_unsafe,
                    latency_ms, prompt_tokens, completion_tokens
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    trace_id,
                    pipeline_version,
                    intent,
                    question,
                    generated_sql,
                    sql_retries,
                    blocked_unsafe,
                    total_ms,
                    prompt_tokens,
                    completion_tokens,
                ),
            )
            conn.commit()

        if self._langfuse_ready():
            try:
                now = datetime.now(timezone.utc)
                t_start = now - timedelta(milliseconds=max(1, total_ms))
                iso_now = now.isoformat().replace("+00:00", "Z")
                iso_start = t_start.isoformat().replace("+00:00", "Z")
                tags = [f"bi-{pipeline_version}", intent]
                if blocked_unsafe:
                    tags.append("blocked_unsafe")
                if root_cause_code:
                    tags.append(root_cause_code)
                batch = [
                    {
                        "id": uuid.uuid4().hex,
                        "type": "trace-create",
                        "timestamp": iso_now,
                        "body": {
                            "id": trace_id,
                            "timestamp": iso_start,
                            "name": f"ecom-bi-{intent}-{pipeline_version}",
                            "input": {"question": question},
                            "output": {"answer": answer, "sql": generated_sql, "root_cause": root_cause_code},
                            "tags": tags,
                            "metadata": {
                                "pipeline_version": pipeline_version,
                                "intent": intent,
                                "sql_retries": sql_retries,
                                "blocked_unsafe": blocked_unsafe,
                                "total_ms": total_ms,
                            },
                        },
                    },
                    {
                        "id": uuid.uuid4().hex,
                        "type": "generation-create",
                        "timestamp": iso_now,
                        "body": {
                            "id": uuid.uuid4().hex,
                            "traceId": trace_id,
                            "name": f"bi_{intent}_llm",
                            "model": self._settings.llm_model,
                            "startTime": iso_start,
                            "endTime": iso_now,
                            "input": question,
                            "output": answer,
                            "usage": {
                                "input": prompt_tokens,
                                "output": completion_tokens,
                                "total": prompt_tokens + completion_tokens,
                                "unit": "TOKENS",
                            },
                        },
                    },
                ]
                self._post_langfuse(batch)
            except Exception:
                pass

        return trace_id

    def record_feedback(self, trace_id: str, user_feedback: int, badcase_tag: str | None = None) -> int:
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ecom_bi.bi_audit_logs
                SET user_feedback = %s, badcase_tag = %s
                WHERE trace_id = %s;
                """,
                (user_feedback, badcase_tag, trace_id),
            )
            updated = cur.rowcount
            conn.commit()

        if updated > 0 and self._langfuse_ready():
            try:
                iso_now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                self._post_langfuse(
                    [
                        {
                            "id": uuid.uuid4().hex,
                            "type": "score-create",
                            "timestamp": iso_now,
                            "body": {
                                "id": uuid.uuid4().hex,
                                "traceId": trace_id,
                                "name": "user_feedback",
                                "value": 1.0 if user_feedback > 0 else 0.0,
                                "comment": badcase_tag or "",
                            },
                        }
                    ]
                )
            except Exception:
                pass
        return updated
