## 2026-07-03 - Focus Restoration on Rerendered Lists
**Learning:** In SPAs where list items are dynamically re-rendered (e.g. by background polling), storing a direct DOM node reference for focus restoration fails because the node gets detached.
**Action:** Use data attributes (e.g. `data-id`) on the dynamic list items. Upon closing a detailed view, fallback to finding the recreated element via `document.querySelector` using the stored ID to successfully restore focus.
