# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Heart & Critique (EAS-free Web2.5 Edition)** — a Python serverless AI agent combining LLM-driven story generation, real-time news search, and x402 Web3 payment gating. Deployed on Vercel; all logic lives in a single handler file.

## Development Commands

```bash
# Run locally (defaults to port 9999)
python agent.py
python agent.py 8080

# Test the agent endpoint
curl -X POST http://localhost:9999 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","params":{"message":{"parts":[{"text":"이야기 해줘"}]}},"id":1}'

# Check agent card
curl http://localhost:9999/.well-known/agent-card.json
```

No build step, no linter, no test suite. All dependencies are Python stdlib only — `requirements.txt` is intentionally empty.

## Architecture

The entire agent is implemented in **`api/index.py`** (single-file serverless handler). `agent.py` wraps it in a local HTTP server for development.

### Request Routing

All HTTP requests are rewritten to `api/index.py` via `vercel.json`. The handler dispatches by path:
- `GET /` → serves the embedded HTML frontend (818+ lines of HTML/JS/CSS inlined in `get_html()`)
- `GET /.well-known/agent-card.json` → A2A agent metadata
- `POST /` → JSON-RPC 2.0 dispatch

### JSON-RPC Methods

| Method | Cost | Description |
|---|---|---|
| `message/send` | Free | Generates kindness or critique story (50/50 random) |
| `sources/reveal` | x402 USDC | Decrypts and returns source citations |

### LLM Pipelines

Two mutually exclusive pipelines selected by which API key is set:

**Groq mode** (default, requires `GROQ_API_KEY` + `TAVILY_API_KEY`):
1. Tavily search with curated domain allow-list
2. Llama model generates story from search results
3. `USED_SOURCES: url1,url2` meta-line extracted from LLM output via regex

**Gemini mode** (requires `GEMINI_API_KEY`):
1. Gemini with Google Search grounding (built-in)
2. Grounding metadata provides citations directly

### Payment Flow (x402)

1. `message/send` returns story + encrypted source token (SHAKE-256 + HMAC-SHA256)
2. Client pays via EIP-3009 USDC authorization signature on Base/Base Sepolia
3. `sources/reveal` verifies payment through facilitator, decrypts token, returns sources
4. Optional auto-settlement configured via `X402_FACILITATOR`

### Frontend

Vanilla JS + HTML inlined in `get_html()`. Uses the `viem` library (loaded from CDN) for wallet integration. Two-phase UX: free story generation → paid source reveal.

## Required Environment Variables

| Variable | Required For | Notes |
|---|---|---|
| `GROQ_API_KEY` | Groq pipeline | Default pipeline |
| `TAVILY_API_KEY` | Groq pipeline | News search |
| `GEMINI_API_KEY` | Gemini pipeline | Alternative to Groq |
| `X402_PAY_TO` | Payment gating | Wallet address to receive payment |
| `X402_NETWORK` | Payment gating | `base` or `base-sepolia` |
| `X402_AMOUNT` | Optional | Defaults to minimum |
| `X402_FACILITATOR` | Optional | Payment settlement endpoint |
| `TAVILY_INCLUDE_DOMAINS` | Optional | Override domain allow-list |

## Key Design Constraints

- **No external dependencies**: Must stay pure Python stdlib. Any new functionality must avoid adding packages to `requirements.txt`.
- **Single-file handler**: All server logic stays in `api/index.py`. Do not split into modules — Vercel's Python runtime expects this layout.
- **Korean content**: Story prompts, UI copy, and comments are in Korean. The agent targets Korean-language content.
- **60-second max duration**: Vercel enforces this. LLM + search calls must complete within budget.
