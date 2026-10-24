"""Durable PostgreSQL queue for Agnes only. Configured with AGNES_DATABASE_URL."""
from __future__ import annotations
import os
from datetime import datetime, timezone
from psycopg import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

def _connect():
    dsn = os.environ.get("AGNES_DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("AGNES_DATABASE_URL is missing")
    return connect(dsn, row_factory=dict_row, connect_timeout=10)

def initialize_schema():
    """Apply only the Agnes schema after verifying the database identity."""
    expected = "agnes_music_video_db"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS name")
            if cur.fetchone()["name"] != expected:
                raise RuntimeError("Refusing to migrate a database that is not dedicated to Agnes")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.tasks (
                  id text PRIMARY KEY, user_id text NOT NULL DEFAULT '',
                  type text NOT NULL,
                  status text NOT NULL CHECK(status IN ('queued','processing','completed','failed','cancelled')),
                  progress double precision NOT NULL DEFAULT 0,
                  input jsonb NOT NULL,
                  dir_name text NOT NULL,
                  result_url text NOT NULL DEFAULT '',
                  error text NOT NULL DEFAULT '',
                  created_at timestamptz NOT NULL DEFAULT now(),
                  started_at timestamptz,
                  completed_at timestamptz,
                  updated_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS agnes_tasks_status_created ON public.tasks(status,created_at)")


def create_task(task_id, task_type, input_data, dir_name, user_id=""):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO tasks (id,user_id,type,input,dir_name,status,progress)
                VALUES (%s,%s,%s,%s,%s,'queued',0)
                RETURNING *""", (task_id,user_id,task_type,Jsonb(input_data),dir_name))
            return cur.fetchone()

def get_task(task_id):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
            return cur.fetchone()

def list_tasks(limit=0,offset=0,status=""):
    filters=[x.strip() for x in status.split(",") if x.strip()]
    with _connect() as conn:
        with conn.cursor() as cur:
            where="WHERE status = ANY(%s)" if filters else ""
            args=(filters,) if filters else ()
            cur.execute("SELECT count(*) AS n FROM tasks "+where, args)
            total=cur.fetchone()["n"]
            cur.execute("SELECT * FROM tasks "+where+" ORDER BY created_at DESC LIMIT %s OFFSET %s",
                        (*args,limit or 2147483647,offset))
            return cur.fetchall(),total

_ALLOWED_FIELDS={"status","progress","result_url","error","started_at","completed_at","updated_at"}
def update_task(task_id, **fields):
    if set(fields)-_ALLOWED_FIELDS:
        raise ValueError("Unsupported task update fields")
    if not fields:
        return get_task(task_id)
    fields["updated_at"]=datetime.now(timezone.utc)
    keys=list(fields)
    sql="UPDATE tasks SET "+", ".join(f"{k}=%s" for k in keys)+" WHERE id=%s RETURNING *"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql,(*[fields[k] for k in keys],task_id))
            return cur.fetchone()

def claim_next_task():
    # SKIP LOCKED and UPDATE are one transaction; competing workers never claim one job.
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""WITH next AS (
                SELECT id FROM tasks WHERE status='queued'
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE tasks AS t SET status='processing',started_at=now(),updated_at=now()
                FROM next WHERE t.id=next.id RETURNING t.*""")
            return cur.fetchone()
