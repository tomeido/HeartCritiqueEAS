## 2024-05-19 - Restoring Focus to Dynamically Re-rendered Elements
**Learning:** In a single-page application where list items trigger detail views and the list might re-render while the detail is open, storing DOM node references to restore focus upon closing the detail view is fragile. Nodes may detach, causing focus restoration to fail silently.
**Action:** Always attach a stable, unique `data-id` attribute to list items and use `document.querySelector` with that ID to query for the fresh DOM node and call `.focus({ preventScroll: true })` on it when the detail view closes.

## 2025-02-24 - Inline Loading States and Keyboard Discoverability
**Learning:** Adding a spinner to the search icon itself during an API call provides clear, immediate feedback without disrupting the layout. Additionally, indicating keyboard shortcuts in placeholders or tooltips (e.g. `/` for search and `Esc` for closing) improves discoverability for keyboard users.
**Action:** Always consider using existing nearby icons for inline loading states rather than creating new spinner elements that may cause layout shifts. Provide keyboard shortcut hints in UI text whenever implementing global keyboard event listeners.

## 2024-05-18 - Managing Focus in Dynamic Empty States
**Learning:** In SPAs where UI elements completely disappear upon interaction (e.g. clicking "Clear Search" or "Return to All" within an empty state view), native keyboard and screen reader focus is lost because the DOM node is destroyed.
**Action:** Always programmatically move focus (`.focus({preventScroll: true})`) to the logical next element (e.g., the search input, the filter chip, or a main CTA button) inside the `onclick` handler right before or via a `setTimeout` after triggering the re-render. Make sure target containers have `tabindex="-1"` and `outline: none` so they can receive programmatic focus without showing an ugly visual ring.
