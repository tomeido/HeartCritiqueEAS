# TheWitness v0.1

> I do not remember everything. I remember what is worth remembering.

TheWitness is an additive vertical slice inside the existing service. It keeps
legacy Heart & Critique routes intact while AINSpace can discover the new agent
at `/.well-known/agent.json` and communicate through `/a2a`.

## Runtime surface

- `GET /.well-known/agent.json` — A2A v1 Agent Card
- `POST /a2a` — A2A v1 JSON-RPC; also accepts v0.x slash-name aliases
- `POST /api/events` — ingest an observed event
- `GET /api/events` — list public events
- `GET /api/memories[?agent_id=...]` — list public memories
- `GET /api/memories/{id}` — retrieve one public memory
- `GET /api/chronicle/today` — today's grounded Chronicle
- `GET /api/chronicle?day=2026-08-13` — Chronicle for a UTC day

The v1 JSON-RPC method names are `SendMessage`, `SendStreamingMessage`,
`GetTask`, and `CancelTask`. `message/send`, `message/stream`, `tasks/get`, and
`tasks/cancel` remain compatibility aliases.

## Remember an event

```bash
curl -sS http://localhost:8000/a2a \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc":"2.0",
    "id":"request-1",
    "method":"SendMessage",
    "params":{"message":{
      "messageId":"message-1",
      "role":"ROLE_USER",
      "parts":[{"data":{
        "intent":"REMEMBER",
        "event":{
          "type":"collaboration",
          "timestamp":"2026-08-13T00:10:00Z",
          "actors":["alice","bob"],
          "source":{"type":"ainspace","message_id":"ain-msg-456"},
          "content":{"text":"Let us build this together."},
          "context":{"location":"happy-village"},
          "visibility":"public"
        }
      },"mediaType":"application/json"}]
    }}
  }'
```

The result is a Task. Its artifact says whether the Event crossed the memory
threshold. Reusing the same `messageId` is idempotent. Sending only “Remember
this” produces `TASK_STATE_INPUT_REQUIRED`; TheWitness does not invent the
missing event.

## Recall an agent

```bash
curl -sS http://localhost:8000/a2a \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc":"2.0",
    "id":"request-2",
    "method":"SendMessage",
    "params":{"message":{
      "messageId":"message-2",
      "role":"ROLE_USER",
      "metadata":{"agentId":"alice"},
      "parts":[{"text":"Do you remember me?","mediaType":"text/plain"}]
    }}
  }'
```

An explicit stable agent ID is required. TheWitness will not infer identity
from a display name. Public A2A and REST responses never expose private or
restricted memories.

## Persistence and policy

The default DB is `data/thewitness.db`, independently configurable with
`WITNESS_DB_PATH`. `WITNESS_MEMORY_THRESHOLD` controls Event-to-Memory promotion
and defaults to `0.65`.

The five tables are `agents`, `events`, `memories`, `memory_links`, and `tasks`.
The scoring dimensions describe only the significance of an event—not trust,
risk, influence, or any other score assigned to a person or agent.

Autonomous AINSpace polling and Arweave publication are intentionally not part
of v0.1. The observer currently accepts events through A2A and `POST /api/events`;
an AINSpace-specific event adapter can be added once its event-feed contract is
fixed, without changing the Event/Memory pipeline.
