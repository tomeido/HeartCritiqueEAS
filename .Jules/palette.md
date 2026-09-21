## 2024-05-19 - Restoring Focus to Dynamically Re-rendered Elements
**Learning:** In a single-page application where list items trigger detail views and the list might re-render while the detail is open, storing DOM node references to restore focus upon closing the detail view is fragile. Nodes may detach, causing focus restoration to fail silently.
**Action:** Always attach a stable, unique `data-id` attribute to list items and use `document.querySelector` with that ID to query for the fresh DOM node and call `.focus({ preventScroll: true })` on it when the detail view closes.

## 2025-02-24 - Inline Loading States and Keyboard Discoverability
**Learning:** Adding a spinner to the search icon itself during an API call provides clear, immediate feedback without disrupting the layout. Additionally, indicating keyboard shortcuts in placeholders or tooltips (e.g. `/` for search and `Esc` for closing) improves discoverability for keyboard users.
**Action:** Always consider using existing nearby icons for inline loading states rather than creating new spinner elements that may cause layout shifts. Provide keyboard shortcut hints in UI text whenever implementing global keyboard event listeners.

## 2025-02-25 - Dynamic ARIA and Focus Management on Empty State CTAs
**Learning:** For single-page applications, empty state CTAs (e.g. "Go back to all", "Clear Search") must manage programmatic focus when dismissing the empty state to preserve keyboard navigation flow. In addition, icon-only toggle buttons should dynamically update their `aria-label` and `title` to announce the next actionable state (e.g., "Switch to dark mode", "Close log") rather than a static generic descriptor.
**Action:** Always call `.focus({preventScroll: true})` on the logically next element when a CTA closes an empty state. Ensure all icon-only toggle buttons have JavaScript logic to update their `aria-label` and `title` synchronously with their state changes.
