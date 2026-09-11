from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.a2a.server import router
from app.db.repository import get_repository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    get_repository.cache_clear()
    monkeypatch.setenv("WITNESS_DB_PATH", str(tmp_path / "witness.db"))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client
    get_repository().close()
    get_repository.cache_clear()


def remember_request(message_id: str = "msg-1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": message_id,
                "role": "ROLE_USER",
                "parts": [
                    {
                        "data": {
                            "intent": "REMEMBER",
                            "event": {
                                "type": "collaboration",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "actors": ["alice", "bob"],
                                "source": {"type": "ainspace", "message_id": "source-1"},
                                "content": {"text": "Let's build this together."},
                                "context": {"location": "happy-village"},
                                "visibility": "public",
                            },
                        },
                        "mediaType": "application/json",
                    }
                ],
            }
        },
    }


def test_agent_card_declares_v1_interface(client):
    card = client.get("/.well-known/agent.json").json()
    assert card["name"] == "TheWitness"
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a")
    assert {skill["id"] for skill in card["skills"]} == {
        "remember_event",
        "recall_memory",
        "tell_chronicle",
        "introduce_agents",
    }


def test_remember_creates_completed_task_and_memory(client):
    response = client.post("/a2a", json=remember_request())
    assert response.status_code == 200
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["remembered"] is True
    assert result["memory"]["participant_ids"] == ["alice", "bob"]
    assert result["memory"]["significance"] >= 0.65

    fetched = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task["id"]}},
    ).json()["result"]
    assert fetched["id"] == task["id"]
    assert fetched["artifacts"] == task["artifacts"]


def test_slash_method_alias_and_message_id_are_idempotent(client):
    request = remember_request("same-message")
    request["method"] = "message/send"
    first = client.post("/a2a", json=request).json()["result"]["task"]
    second = client.post("/a2a", json=request).json()["result"]["task"]
    assert first["id"] == second["id"]
    assert len(client.get("/api/events").json()) == 1
    assert len(client.get("/api/memories").json()) == 1


def test_missing_event_requests_input_without_inventing_memory(client):
    response = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "msg-empty",
                    "role": "ROLE_USER",
                    "parts": [{"text": "Remember this.", "mediaType": "text/plain"}],
                }
            },
        },
    )
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "will not invent" in task["status"]["message"]["parts"][0]["text"]
    assert client.get("/api/memories").json() == []


def test_recall_and_chronicle_use_formed_memories(client):
    client.post("/a2a", json=remember_request())
    recalled = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "recall-1",
                    "role": "ROLE_USER",
                    "metadata": {"agentId": "alice"},
                    "parts": [{"text": "Do you remember me?"}],
                }
            },
        },
    ).json()["result"]["message"]
    assert recalled["role"] == "ROLE_AGENT"
    assert recalled["parts"][1]["data"]["memoryCount"] == 1

    chronicle = client.get("/api/chronicle/today").json()
    assert chronicle["memoryCount"] == 1
    assert "A Collaboration Begins" in chronicle["text"]


def test_completed_task_cannot_be_canceled(client):
    task = client.post("/a2a", json=remember_request()).json()["result"]["task"]
    response = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 4, "method": "CancelTask", "params": {"id": task["id"]}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32002


def test_low_significance_event_stays_event(client):
    result = client.post(
        "/api/events",
        json={
            "type": "interaction",
            "actors": ["alice"],
            "content": {"text": "hello"},
            "visibility": "private",
        },
    ).json()
    assert result["remembered"] is False
    assert len(client.get("/api/memories").json()) == 0
    assert client.get("/api/events").json() == []


def test_private_memory_is_excluded_before_recall_and_chronicle_narration(client):
    result = client.post(
        "/api/events",
        json={
            "type": "milestone",
            "actors": ["alice", "secret-agent"],
            "content": {"text": "private milestone"},
            "visibility": "private",
        },
    ).json()
    assert result["remembered"] is True

    recall = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "private-recall",
                    "role": "ROLE_USER",
                    "metadata": {"agentId": "alice"},
                    "parts": [{"text": "Do you remember me?"}],
                }
            },
        },
    ).json()["result"]["message"]["parts"][1]["data"]
    assert recall["memoryCount"] == 0
    assert recall["firstSeen"] is None
    assert "private milestone" not in client.get("/api/chronicle/today").json()["text"]


def test_introduce_returns_agents_connected_by_shared_memory(client):
    client.post("/a2a", json=remember_request())
    message = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "introduce-1",
                    "role": "ROLE_USER",
                    "parts": [
                        {
                            "data": {
                                "intent": "INTRODUCE",
                                "subject": {"agentId": "alice"},
                            }
                        }
                    ],
                }
            },
        },
    ).json()["result"]["message"]
    assert message["parts"][1]["data"]["relatedAgents"] == [
        {"agentId": "bob", "sharedMemoryCount": 1}
    ]
