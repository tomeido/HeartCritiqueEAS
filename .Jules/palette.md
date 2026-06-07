## 2024-05-24 - Interactive Element States & Context
**Learning:** Buttons that lose their text labels during loading states not only cause jarring layout shifts (shrinking) but also completely lose their context for screen reader users. Filter button groups need explicit ARIA states (`aria-pressed`) to convey their toggle nature to assistive technologies.
**Action:** Always include a descriptive text alongside the loading spinner (e.g., `<span class="spin"></span> 진행 중...`) to maintain layout stability and accessibility. Add `aria-pressed` to interactive filter chips.
