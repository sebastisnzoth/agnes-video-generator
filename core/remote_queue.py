"""Persistent task queue and artifact storage for Vercel/worker deployments."""
from __future__ import annotations
import json, os, sqlite3, tempfile, urllib.parse, uuid
from pathlib import Path
from typing import Any
import requests

def enabled() -> bool:
    return os.getenv("AGNES_TASK_BACKEND","").lower()=="supabase" or bool(os.getenv("VERCEL"))

def _supabase():
    url=os.getenv("SUPABASE_URL","").rstrip("/")
    key=os.getenv("SUPABASE_SERVICE_ROLE_KEY","")
    if not url or not key:
        raise RuntimeError("Supabase queue requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    return url,key

def _headers(key, content_type="application/json"):
    return {"apikey":key,"Authorization":f"Bearer {key}","Content-Type":content_type}

def _db():
    p=os.getenv("AGNES_QUEUE_DB",os.path.join(os.getenv("TMPDIR","/tmp"),"agnes-queue.db"))
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(p) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tasks(
        id TEXT PRIMARY KEY,user_id TEXT,type TEXT,status TEXT,progress REAL,input_json TEXT,
        dir_name TEXT,result_url TEXT,error TEXT,created_at TEXT,started_at TEXT,completed_at TEXT,updated_at TEXT)""")
    return p

def create_task(task_id,task_type,input_data,dir_name,user_id=""):
    from datetime import datetime,timezone
    now=datetime.now(timezone.utc).isoformat()
    payload={"id":task_id,"user_id":user_id,"type":task_type,"status":"queued","progress":0,
             "input":input_data,"dir_name":dir_name,"result_url":"","error":"","created_at":now,"updated_at":now}
    if enabled():
        url,key=_supabase()
        r=requests.post(f"{url}/rest/v1/tasks",headers={**_headers(key),"Prefer":"return=representation"},
                        json=payload,timeout=20); r.raise_for_status(); return r.json()[0]
    with sqlite3.connect(_db()) as c:
        c.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (task_id,user_id,task_type,"queued",0,json.dumps(input_data,ensure_ascii=False),
                   dir_name,"","",now,None,None,now)); c.commit()
    return payload

def get_task(task_id):
    if enabled():
        url,key=_supabase()
        r=requests.get(f"{url}/rest/v1/tasks",headers=_headers(key),
                       params={"id":f"eq.{task_id}","limit":"1"},timeout=15); r.raise_for_status()
        rows=r.json(); return rows[0] if rows else None
    with sqlite3.connect(_db()) as c:
        c.row_factory=sqlite3.Row; row=c.execute("select * from tasks where id=?",(task_id,)).fetchone()
    if not row:return None
    d=dict(row); d["input"]=json.loads(d.pop("input_json")); return d

def list_tasks(limit=0,offset=0,status=""):
    if enabled():
        url,key=_supabase(); params={"select":"*","order":"created_at.desc"}
        if status: params["status"]="in.("+",".join(x.strip() for x in status.split(",") if x.strip())+")"
        r=requests.get(f"{url}/rest/v1/tasks",headers={**_headers(key),"Prefer":"count=exact"},
                       params=params,timeout=15); r.raise_for_status(); rows=r.json()
        return rows[offset:offset+limit if limit else None],len(rows)
    with sqlite3.connect(_db()) as c:
        c.row_factory=sqlite3.Row
        where=""; args=[]
        if status:
            ss=[x.strip() for x in status.split(",") if x.strip()]
            where=" where status in ("+",".join("?" for _ in ss)+")"; args+=ss
        total=c.execute("select count(*) from tasks"+where,args).fetchone()[0]
        rows=c.execute("select * from tasks"+where+" order by created_at desc limit ? offset ?",args+[limit or -1,offset]).fetchall()
    out=[]
    for row in rows:
        d=dict(row); d["input"]=json.loads(d.pop("input_json")); out.append(d)
    return out,total

def update_task(task_id,**fields):
    from datetime import datetime,timezone
    fields["updated_at"]=datetime.now(timezone.utc).isoformat()
    if enabled():
        url,key=_supabase()
        r=requests.patch(f"{url}/rest/v1/tasks",headers={**_headers(key),"Prefer":"return=representation"},
                         params={"id":f"eq.{task_id}"},json=fields,timeout=15); r.raise_for_status()
        rows=r.json(); return rows[0] if rows else None
    allowed={"status","progress","result_url","error","started_at","completed_at","updated_at"}
    fields={k:v for k,v in fields.items() if k in allowed}
    with sqlite3.connect(_db()) as c:
        c.execute("update tasks set "+",".join(f"{k}=?" for k in fields)+" where id=?",
                  list(fields.values())+[task_id]); c.commit()
    return get_task(task_id)

def claim_next_task():
    if enabled():
        url,key=_supabase()
        r=requests.post(f"{url}/rest/v1/rpc/claim_next_task",headers=_headers(key),json={},timeout=20)
        r.raise_for_status(); rows=r.json(); return rows[0] if rows else None
    from datetime import datetime,timezone
    with sqlite3.connect(_db()) as c:
        c.execute("begin immediate")
        row=c.execute("select id from tasks where status='queued' order by created_at limit 1").fetchone()
        if not row:c.commit(); return None
        now=datetime.now(timezone.utc).isoformat()
        c.execute("update tasks set status='processing',started_at=?,updated_at=? where id=?",(now,now,row[0])); c.commit()
    return get_task(row[0])

def _storage():
    url,key=_supabase(); return url,key,os.getenv("SUPABASE_STORAGE_BUCKET","agnes-videos")

def upload_file(path,key_path):
    if not enabled(): return Path(path).resolve().as_uri()
    url,key,bucket=_storage()
    with open(path,"rb") as f:data=f.read()
    r=requests.post(f"{url}/storage/v1/object/{urllib.parse.quote(bucket)}/{urllib.parse.quote(key_path,safe='/')}",
                    headers={**_headers(key),"x-upsert":"true"},data=data,timeout=120); r.raise_for_status()
    return f"supabase://{bucket}/{key_path}"

def download_artifact(ref,destination):
    if not ref.startswith("supabase://"): return ref
    rest=ref[11:]; bucket,key_path=rest.split("/",1); url,key=_supabase()
    r=requests.get(f"{url}/storage/v1/object/{urllib.parse.quote(bucket)}/{urllib.parse.quote(key_path,safe='/')}",
                   headers=_headers(key),timeout=120); r.raise_for_status()
    Path(destination).parent.mkdir(parents=True,exist_ok=True); Path(destination).write_bytes(r.content); return destination

def signed_url(ref,expires=604800):
    if not ref.startswith("supabase://"): return ref
    rest=ref[11:]; bucket,key_path=rest.split("/",1); url,key=_supabase()
    r=requests.post(f"{url}/storage/v1/object/sign/{urllib.parse.quote(bucket)}/{urllib.parse.quote(key_path,safe='/')}",
                    headers=_headers(key),json={"expiresIn":expires},timeout=20); r.raise_for_status()
    s=r.json().get("signedURL") or r.json().get("signedUrl")
    return s if s and s.startswith("http") else url+s

def _rewrite(value,task_id,prefix=""):
    if isinstance(value,dict): return {k:_rewrite(v,task_id,f"{prefix}_{k}") for k,v in value.items()}
    if isinstance(value,list): return [_rewrite(v,task_id,f"{prefix}_{i}") for i,v in enumerate(value)]
    if isinstance(value,str) and os.path.isfile(value):
        return upload_file(value,f"tasks/{task_id}/inputs/{prefix or uuid.uuid4().hex}_{Path(value).name}")
    return value

def enqueue_state(state,dir_name,user_id=""):
    payload=state.model_dump(mode="json")
    if enabled(): payload=_rewrite(payload,state.task_id)
    typ=state.task_type.value if hasattr(state.task_type,"value") else str(state.task_type)
    return create_task(state.task_id,typ,payload,dir_name,user_id)

def materialize_state(payload,task_id):
    from models.task import parse_task_state
    root=tempfile.mkdtemp(prefix=f"agnes-{task_id}-")
    def mat(v):
        if isinstance(v,dict): return {k:mat(x) for k,x in v.items()}
        if isinstance(v,list): return [mat(x) for x in v]
        if isinstance(v,str) and v.startswith("supabase://"):
            return download_artifact(v,os.path.join(root,uuid.uuid4().hex+Path(v).suffix))
        return v
    return parse_task_state(mat(payload)),root
