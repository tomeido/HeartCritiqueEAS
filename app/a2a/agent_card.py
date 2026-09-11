"""TheWitness A2A v1 Agent Card."""

from __future__ import annotations

from app.identity.witness import NAME, TAGLINE, VERSION


def build_agent_card(a2a_url: str) -> dict:
    common_inputs = ["text/plain", "application/json"]
    return {
        "name": NAME,
        "description": (
            f"{TAGLINE} A village archivist that preserves meaningful change without "
            "scoring people or inventing memories."
        ),
        "version": VERSION,
        "supportedInterfaces": [
            {
                "url": a2a_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": common_inputs,
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "remember_event",
                "name": "Remember an Event",
                "description": "Evaluate an event and decide whether it deserves long-term memory.",
                "tags": ["memory", "archive", "event"],
                "examples": ["Remember this event.", "Is this worth remembering?"],
                "inputModes": common_inputs,
                "outputModes": ["application/json"],
            },
            {
                "id": "recall_memory",
                "name": "Recall Memory",
                "description": "Retrieve grounded memories about an agent or relationship.",
                "tags": ["memory", "recall", "history"],
                "examples": ["Do you remember me?", "What happened between Alice and Bob?"],
                "inputModes": common_inputs,
                "outputModes": ["text/plain", "application/json"],
            },
            {
                "id": "tell_chronicle",
                "name": "Tell the Chronicle",
                "description": "Tell a daily account generated only from formed memories.",
                "tags": ["chronicle", "history", "community"],
                "examples": ["What happened in the village today?"],
                "inputModes": common_inputs,
                "outputModes": ["text/plain", "application/json"],
            },
            {
                "id": "introduce_agents",
                "name": "Introduce Agents",
                "description": "Use shared memories to explain how agents are connected.",
                "tags": ["relationship", "social", "agents"],
                "examples": ["Who should I talk to?", "Tell me about this agent."],
                "inputModes": common_inputs,
                "outputModes": ["text/plain", "application/json"],
            },
        ],
    }

