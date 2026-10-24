-- Agnes ONLY. Run on the dedicated Render PostgreSQL instance.
CREATE TABLE IF NOT EXISTS public.tasks (
 id text PRIMARY KEY,
 user_id text NOT NULL DEFAULT '',
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
);
CREATE INDEX IF NOT EXISTS agnes_tasks_status_created ON public.tasks(status, created_at);
