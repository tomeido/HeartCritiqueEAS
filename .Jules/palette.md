## 2026-06-17 - Improve screen reader accessibility for loading states and icons
**Learning:** Screen readers announce purely decorative icons, which causes noise and confusion. During loading states, buttons lacking descriptive text inside (such as only containing a spinner) can leave screen reader users unaware of the action being processed.
**Action:** Use `aria-hidden="true"` on purely decorative icons. Include visually hidden text or update the button text to clearly state the action (e.g., '투표 중...') when an action is processing, and set `aria-busy="true"` on the processing element.
