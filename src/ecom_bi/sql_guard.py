"""基于 sqlglot AST 的只读 SQL 安全护栏与 PostgreSQL 受控执行器。"""

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import sqlglot
from sqlglot import exp

from ecom_bi.config import Settings
from ecom_bi.semantic_layer import SemanticLayer

# 绝对禁止出现在 AST 中的写操作/DDL/权限/命令节点类型
_FORBIDDEN_AST_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Command,
    exp.Copy,
    exp.Merge,
    exp.TruncateTable,
)

# 禁止调用的高危 PostgreSQL 函数（防 DoS、读文件、跨库攻击）
_FORBIDDEN_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "dblink",
    "lo_import",
    "lo_export",
}


@dataclass
class SQLValidationResult:
    """SQL AST 安全校验结果。"""

    is_safe: bool
    safe_sql: str | None = None
    reason: str | None = None
    referenced_tables: list[str] = field(default_factory=list)


@dataclass
class SQLExecutionResult:
    """SQL 受控执行结果。"""

    ok: bool
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    exec_ms: int = 0
    is_empty_or_all_null: bool = False
    error: str | None = None


def validate_and_rewrite_sql(
    raw_sql: str,
    allowed_tables: set[str],
    max_rows: int = 200,
) -> SQLValidationResult:
    """用 sqlglot 解析 AST：强制单句只读 SELECT、校验表白名单、拦截高危函数并自动限制返回行数。"""
    cleaned = (raw_sql or "").strip()
    cleaned = cleaned.removeprefix("```sql").removeprefix("```").removesuffix("```").strip()
    if not cleaned:
        return SQLValidationResult(is_safe=False, reason="SQL 语句为空。")

    try:
        statements = [s for s in sqlglot.parse(cleaned, read="postgres") if s is not None]
    except Exception as exc:
        return SQLValidationResult(is_safe=False, reason=f"SQL 语法解析失败：{exc}")

    if len(statements) != 1:
        return SQLValidationResult(
            is_safe=False,
            reason=f"禁止多语句执行（检测到 {len(statements)} 条语句，防 SQL 注入）。",
        )

    stmt = statements[0]

    # 顶层只允许 SELECT 或 UNION/INTERSECT/EXCEPT 集合查询
    if not isinstance(stmt, (exp.Select, exp.Union)):
        return SQLValidationResult(
            is_safe=False,
            reason=f"仅允许只读 SELECT 查询，已拦截危险操作类型：{type(stmt).__name__}。",
        )

    # 深度遍历整棵 AST，阻止任何嵌套写操作（如 WITH ... AS (DELETE ...) SELECT）
    for forbidden_type in _FORBIDDEN_AST_NODES:
        if stmt.find(forbidden_type) is not None:
            return SQLValidationResult(
                is_safe=False,
                reason=f"检测到禁止的写操作或 DDL 节点：{forbidden_type.__name__}。",
            )

    # 拦截高危系统函数
    for fn in stmt.find_all(exp.Anonymous):
        if fn.name.lower() in _FORBIDDEN_FUNCTIONS:
            return SQLValidationResult(is_safe=False, reason=f"检测到禁止调用的高危函数：{fn.name}")
    for fn in stmt.find_all(exp.Func):
        fname = (fn.sql_name() or "").lower()
        if fname in _FORBIDDEN_FUNCTIONS:
            return SQLValidationResult(is_safe=False, reason=f"检测到禁止调用的高危函数：{fname}")

    # 收集 CTE 名称，避免把 CTE 别名误判为非法物理表
    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE) if cte.alias_or_name}

    # 校验所有真实引用的物理表必须在白名单内
    referenced: list[str] = []
    for tbl in stmt.find_all(exp.Table):
        tname = tbl.name.lower()
        db_schema = tbl.db.lower() if tbl.db else ""
        if not db_schema and tname in cte_names:
            continue
        full_name = f"{db_schema}.{tname}" if db_schema else tname
        referenced.append(full_name)
        if full_name not in allowed_tables:
            return SQLValidationResult(
                is_safe=False,
                reason=f"表 `{full_name}` 不在只读白名单内（禁止跨库/查系统表）。",
                referenced_tables=referenced,
            )

    # 自动追加或收紧 LIMIT，防止大表全量拖垮接口
    limit_node = stmt.args.get("limit")
    if limit_node is None:
        stmt = stmt.limit(max_rows)
    else:
        try:
            limit_expr = limit_node.expression
            if isinstance(limit_expr, exp.Literal) and limit_expr.is_int:
                if int(limit_expr.this) > max_rows:
                    stmt = stmt.limit(max_rows)
        except Exception:
            stmt = stmt.limit(max_rows)

    safe_sql = stmt.sql(dialect="postgres")
    return SQLValidationResult(
        is_safe=True,
        safe_sql=safe_sql,
        referenced_tables=list(dict.fromkeys(referenced)),
    )


def _serialize_cell(val: Any) -> Any:
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    return val


class SafeSQLExecutor:
    """在 PostgreSQL 只读事务与硬超时约束下安全执行已校验 SQL。"""

    def __init__(self, settings: Settings, semantic: SemanticLayer) -> None:
        self._settings = settings
        self._semantic = semantic

    def execute_checked(self, raw_sql: str) -> tuple[SQLValidationResult, SQLExecutionResult | None]:
        validation = validate_and_rewrite_sql(
            raw_sql=raw_sql,
            allowed_tables=self._semantic.allowed_tables,
            max_rows=self._settings.sql_max_rows,
        )
        if not validation.is_safe or not validation.safe_sql:
            return validation, None

        t0 = time.perf_counter()
        try:
            with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
                cur.execute(f"SET LOCAL search_path TO {self._settings.bi_schema};")
                cur.execute("SET LOCAL default_transaction_read_only = ON;")
                cur.execute(f"SET LOCAL statement_timeout = '{self._settings.sql_statement_timeout_ms}ms';")
                cur.execute(validation.safe_sql)
                cols = [desc.name for desc in (cur.description or [])]
                raw_rows = cur.fetchall()

            rows = [
                {col: _serialize_cell(v) for col, v in zip(cols, r, strict=True)}
                for r in raw_rows
            ]
            # 检测零行，或仅 1 行聚合结果但所有字段均为 NULL（如无匹配日期的 SUM）
            is_empty = len(rows) == 0 or (
                len(rows) == 1 and all(v is None for v in rows[0].values())
            )
            return validation, SQLExecutionResult(
                ok=True,
                sql=validation.safe_sql,
                columns=cols,
                rows=rows,
                row_count=len(rows),
                exec_ms=int((time.perf_counter() - t0) * 1000),
                is_empty_or_all_null=is_empty,
            )
        except Exception as exc:
            return validation, SQLExecutionResult(
                ok=False,
                sql=validation.safe_sql,
                exec_ms=int((time.perf_counter() - t0) * 1000),
                error=str(exc),
            )
