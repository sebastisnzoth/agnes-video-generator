"""Remote music task polling must not disclose private object-storage paths."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web.routes import task_routes

def test_remote_task_polling_does_not_leak_song_photo_or_storage_ref(monkeypatch):
    monkeypatch.setattr(task_routes.remote_queue, "enabled", lambda: True)
    record = {
        "id":"music1", "dir_name":"music-dir", "type":"music_video",
        "status":"completed", "progress":1,
        "result_url":"supabase://private/secret-result.mp4",
        "input":{"creative_name":"A song","song_file":"supabase://private/song.mp3",
                 "singer_photo":"supabase://private/person.jpg"},
    }
    monkeypatch.setattr(task_routes.remote_queue,"get_task",lambda _:record)
    monkeypatch.setattr(task_routes.remote_queue,"list_tasks",lambda **_:([record],1))
    app=FastAPI()
    app.include_router(task_routes.router)
    client=TestClient(app)
    detail=client.get("/api/tasks/music1")
    listing=client.get("/api/tasks")
    assert detail.status_code == 200
    assert listing.status_code == 200
    assert detail.json()["result_available"] is True
    assert listing.json()["total"] == 1
    for body in (detail.text,listing.text):
        assert "supabase://" not in body
        assert "secret-result" not in body
        assert "person.jpg" not in body
