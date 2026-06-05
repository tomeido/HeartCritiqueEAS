## 2024-06-05 - Keep accessible name during loading states
**Learning:** When buttons are clicked to trigger async actions (like voting or rechecking), replacing their entire innerHTML with just a spinner icon removes their accessible name. This leaves screen reader users stranded, hearing nothing or just the word "spin" without context of what is loading. It also reduces visual context for all users.
**Action:** Always include descriptive loading text alongside spinner icons (e.g., `<span class="spin"></span> 투표 중...`) to maintain context and accessibility during loading states.
