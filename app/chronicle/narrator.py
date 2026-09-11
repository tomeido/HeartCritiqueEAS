"""Grounded daily chronicle narration.

Only memories are read; raw events are intentionally excluded from narration.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.db.repository import WitnessRepository, get_repository


def daily_chronicle(
    day: date | None = None,
    repository: WitnessRepository | None = None,
    public_only: bool = True,
) -> dict[str, Any]:
    repo = repository or get_repository()
    chosen = day or datetime.now(timezone.utc).date()
    start = datetime.combine(chosen, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    memories = repo.list_memories(since=start.isoformat(), until=end.isoformat(), limit=500)
    if public_only:
        memories = [memory for memory in memories if memory["visibility"] == "public"]
    ranked = sorted(memories, key=lambda item: item["significance"], reverse=True)

    if not ranked:
        text = f"THE WITNESS CHRONICLE\n\n{chosen.isoformat()}\n\nNo event became a memory today."
    else:
        lines = [
            "THE WITNESS CHRONICLE",
            "",
            chosen.isoformat(),
            "",
            f"{len(ranked)} {'thing' if len(ranked) == 1 else 'things'} mattered today.",
            "",
        ]
        for index, memory in enumerate(ranked, 1):
            lines.append(f"{index}. {memory['title']}")
            lines.append(f"   {memory['summary']}")
        lines.extend(["", f"The most significant memory scored {ranked[0]['significance']:.2f}."])
        text = "\n".join(lines)
    return {"date": chosen.isoformat(), "memoryCount": len(ranked), "memories": ranked, "text": text}
