import os
import threading
import snowflake.connector

_conn = None
_conn_lock = threading.Lock()


def _get_conn():
    global _conn
    with _conn_lock:
        if _conn is not None and not _conn.is_closed():
            return _conn
        _conn = None

        kwargs = dict(
            account=os.environ.get("SNOWFLAKE_ACCOUNT", "sza96462.us-east-1"),
            user=os.environ["SNOWFLAKE_USER"],
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", ""),
            database="REPORTING",
            schema="GENERAL",
            role=os.environ.get("SNOWFLAKE_ROLE", ""),
        )
        private_key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
        if private_key_path:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.backends import default_backend
            passphrase_raw = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
            passphrase = passphrase_raw.encode() if passphrase_raw else None
            with open(private_key_path, "rb") as f:
                p_key = serialization.load_pem_private_key(f.read(), password=passphrase, backend=default_backend())
            kwargs["private_key"] = p_key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        else:
            kwargs["authenticator"] = "externalbrowser"
        _conn = snowflake.connector.connect(**kwargs)
        return _conn


def fetch_ticket(ticket_id: int) -> dict:
    sql = """
        SELECT
            t.ID,
            t.SUBJECT,
            t.STATUS,
            t.PRIORITY,
            t.PRIMARY_PRODUCT_COMPONENT,
            t.PRODUCT_COMPONENTS,
            t.CREATED_TIMESTAMP,
            t.SOLVED_TIMESTAMP,
            t.TICKET_COMPLEXITY,
            t.IS_ESCALATED_TO_ENGINEERING,
            t.IS_ESCALATED_TO_JIRA_ESCALATION,
            t.SATISFACTION_RATING_SCORE,
            t.OPEX_DESCRIPTION,
            t.OPEX_REASON,
            t.TICKET_IMPACT,
            c.FULL_CONVERSATION,
            e.SUMMARY,
            e.CUSTOMER_SITUATION,
            e.INVESTIGATION,
            e.SUGGESTED_SOLUTION_BY_AGENTS,
            jira.JIRA_KEY
        FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET t
        LEFT JOIN REPORTING.DLAC_RESTRICTED.FACT_ZENDESK_TICKET_CONVERSATIONS_TECH_SUPPORT_AI c
            ON t.ID = c.TICKET_ID
        LEFT JOIN REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS e
            ON t.ID = e.TICKET_ID
        LEFT JOIN (
            SELECT l.TICKET_ID, MIN(j.KEY) AS JIRA_KEY
            FROM REPORTING.GENERAL.DIM_ZENDESK_JIRA_LINK l
            JOIN REPORTING.GENERAL.DIM_JIRA_ISSUE j ON l.ISSUE_ID = j.ID
            WHERE l.TICKET_ID = %s
            GROUP BY l.TICKET_ID
        ) jira ON t.ID = jira.TICKET_ID
        WHERE t.ID = %s
    """
    conn = _get_conn()
    with conn.cursor(snowflake.connector.DictCursor) as cur:
        cur.execute(sql, (ticket_id, ticket_id))
        row = cur.fetchone()
    if not row:
        return {}
    return {k.lower(): v for k, v in row.items()}



def find_similar_tickets(ticket_id: int, limit: int = 5) -> list:
    sql = """
        SELECT
            candidate.TICKET_ID,
            t.SUBJECT,
            t.STATUS,
            t.SOLVED_TIMESTAMP,
            candidate.PRIMARY_PRODUCT_COMPONENT,
            candidate.CUSTOMER_SITUATION,
            candidate.SUGGESTED_SOLUTION_BY_AGENTS,
            candidate.INVESTIGATION,
            VECTOR_COSINE_SIMILARITY(
                reference.EMBEDDINGS::VECTOR(FLOAT, 1536),
                candidate.EMBEDDINGS::VECTOR(FLOAT, 1536)
            ) AS similarity_score
        FROM REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS reference
        JOIN REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS candidate
            ON candidate.TICKET_ID != reference.TICKET_ID
        JOIN REPORTING.GENERAL.DIM_ZENDESK_TICKET t
            ON candidate.TICKET_ID = t.ID
        WHERE reference.TICKET_ID = %s
        ORDER BY similarity_score DESC
        LIMIT %s
    """
    conn = _get_conn()
    with conn.cursor(snowflake.connector.DictCursor) as cur:
        cur.execute(sql, (ticket_id, limit))
        rows = cur.fetchall()
    return [{k.lower(): v for k, v in row.items()} for row in rows]


def weekly_digest_candidates(component: str = None) -> list:
    where_component = "AND t.PRIMARY_PRODUCT_COMPONENT = %s" if component else ""
    sql = f"""
        SELECT
            t.ID,
            t.SUBJECT,
            t.STATUS,
            t.PRIMARY_PRODUCT_COMPONENT,
            t.TICKET_COMPLEXITY,
            t.IS_ESCALATED_TO_ENGINEERING,
            t.IS_ESCALATED_TO_JIRA_ESCALATION,
            t.TICKET_IMPACT,
            t.OPEX_DESCRIPTION,
            t.OPEX_REASON,
            t.SATISFACTION_RATING_SCORE,
            t.SOLVED_TIMESTAMP,
            e.SUMMARY,
            e.CUSTOMER_SITUATION,
            e.INVESTIGATION,
            e.SUGGESTED_SOLUTION_BY_AGENTS
        FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET t
        LEFT JOIN REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS e
            ON t.ID = e.TICKET_ID
        WHERE t.SOLVED_TIMESTAMP >= DATEADD(day, -90, CURRENT_TIMESTAMP())
            AND t.STATUS IN ('closed', 'solved')
            AND t.PRIMARY_PRODUCT_COMPONENT IS NOT NULL
            {where_component}
        ORDER BY t.SOLVED_TIMESTAMP DESC
        LIMIT 500
    """
    conn = _get_conn()
    with conn.cursor(snowflake.connector.DictCursor) as cur:
        cur.execute(sql, (component,) if component else ())
        rows = cur.fetchall()
    return [{k.lower(): v for k, v in row.items()} for row in rows]


def batch_novelty_check(tickets: list) -> dict:
    """Return {ticket_id: max_cosine_similarity} for a batch in one query."""
    if not tickets:
        return {}
    placeholders = ", ".join("%s" for _ in tickets)
    params = [t["id"] for t in tickets]
    sql = f"""
        SELECT
            candidate.TICKET_ID,
            MAX(VECTOR_COSINE_SIMILARITY(
                candidate.EMBEDDINGS::VECTOR(FLOAT, 1536),
                other.EMBEDDINGS::VECTOR(FLOAT, 1536)
            )) AS similarity
        FROM REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS candidate
        JOIN REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS other
            ON other.TICKET_ID != candidate.TICKET_ID
            AND other.PRIMARY_PRODUCT_COMPONENT = candidate.PRIMARY_PRODUCT_COMPONENT
            AND other.CREATED_TIMESTAMP < DATEADD(day, -7, CURRENT_TIMESTAMP())
            AND other.CREATED_TIMESTAMP >= DATEADD(day, -180, CURRENT_TIMESTAMP())
        WHERE candidate.TICKET_ID IN ({placeholders})
        GROUP BY candidate.TICKET_ID
    """
    conn = _get_conn()
    with conn.cursor(snowflake.connector.DictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return {row["TICKET_ID"]: float(row["SIMILARITY"]) for row in rows}


