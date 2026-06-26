## 2026-06-26 - Add accessible role to filter groups
**Learning:** Filter chips (like categories/tags) implemented as separate buttons are confusing to screen reader users unless properly grouped.
**Action:** Always add `role="group"` and a descriptive `aria-label` to the container wrapping a set of filter buttons.
