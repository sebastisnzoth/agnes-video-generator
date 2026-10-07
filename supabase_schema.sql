create table if not exists public.tasks (
 id text primary key,user_id text,type text not null,
 status text not null check(status in ('queued','processing','completed','failed','cancelled')),
 progress double precision not null default 0,input jsonb not null,dir_name text not null,
 result_url text default '',error text default '',created_at timestamptz not null default now(),
 started_at timestamptz,completed_at timestamptz,updated_at timestamptz not null default now());
create index if not exists tasks_status_created_idx on public.tasks(status,created_at);
create or replace function public.claim_next_task()
returns setof public.tasks language plpgsql security definer as $$
declare claimed public.tasks;
begin
 select * into claimed from public.tasks where status='queued' order by created_at for update skip locked limit 1;
 if claimed.id is null then return; end if;
 update public.tasks set status='processing',started_at=now(),updated_at=now() where id=claimed.id returning * into claimed;
 return next claimed;
end; $$;
