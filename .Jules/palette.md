## 2026-06-11 - Add aria-pressed state to filter chips
**Learning:** Visual-only state changes (like applying an `.active` class to filter chips) are completely hidden from screen readers.
**Action:** When creating toggle buttons or filter chips, always pair visual state classes like `.active` with semantic accessibility attributes like `aria-pressed="true"`/`aria-pressed="false"`, and ensure click handlers update both simultaneously.
