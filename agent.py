"""
Local dev runner. Reuses the same handler that runs on Vercel
(api/index.py) so behavior is identical between local and prod.

Run:
    $env:GEMINI_API_KEY = '...'
    py agent.py                 # 0.0.0.0:9999
    py agent.py 7000            # custom port
"""

import os
import sys
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "api"))

from index import handler as Handler  # noqa: E402


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999

    provider = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
    required = ["GROQ_API_KEY", "TAVILY_API_KEY"] if provider == "groq" else ["GEMINI_API_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"ERROR: required env var(s) not set for LLM_PROVIDER={provider}: "
              f"{', '.join(missing)}.", file=sys.stderr)
        for v in missing:
            print(f"  PowerShell:  $env:{v} = 'your-key'", file=sys.stderr)
        sys.exit(1)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 64)
    print("  Heart & Critique - local A2A dev server")
    print("=" * 64)
    print(f"  A2A URL     : http://localhost:{port}/")
    print(f"  Agent Card  : http://localhost:{port}/.well-known/agent-card.json")
    print(f"  JSON-RPC    : POST http://localhost:{port}/  (method: message/send)")
    print("  Stop with Ctrl+C")
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
