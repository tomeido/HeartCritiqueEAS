## 2026-06-19 - Context-Preserving Loading States
**Learning:** Replacing a button's text entirely with a spinner removes essential context for screen reader users and those with cognitive disabilities, making it unclear what action is in progress.
**Action:** Keep descriptive text (e.g., '투표 중...') alongside the spinner and use `aria-busy="true"` to properly signal the loading state to assistive technologies.
