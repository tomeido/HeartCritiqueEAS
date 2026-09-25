## 2024-05-19 - Restoring Focus to Dynamically Re-rendered Elements
**Learning:** In a single-page application where list items trigger detail views and the list might re-render while the detail is open, storing DOM node references to restore focus upon closing the detail view is fragile. Nodes may detach, causing focus restoration to fail silently.
**Action:** Always attach a stable, unique `data-id` attribute to list items and use `document.querySelector` with that ID to query for the fresh DOM node and call `.focus({ preventScroll: true })` on it when the detail view closes.

## 2025-02-24 - Inline Loading States and Keyboard Discoverability
**Learning:** Adding a spinner to the search icon itself during an API call provides clear, immediate feedback without disrupting the layout. Additionally, indicating keyboard shortcuts in placeholders or tooltips (e.g. `/` for search and `Esc` for closing) improves discoverability for keyboard users.
**Action:** Always consider using existing nearby icons for inline loading states rather than creating new spinner elements that may cause layout shifts. Provide keyboard shortcut hints in UI text whenever implementing global keyboard event listeners.

## 2024-05-20 - Empty State Keyboard Focus and Dynamic ARIA Labels
**Learning:** Empty states with call-to-action buttons (like "Clear Search" or "Back to All") often break keyboard navigation if they don't explicitly manage focus after being clicked. Additionally, toggle buttons (like theme switches) need their `aria-label` and `title` attributes updated dynamically to remain accessible.
**Action:** When an empty state CTA removes the empty state itself, explicitly move focus to the logical next element (e.g., the search input or the container). Always update `aria-label` and `title` via JavaScript for toggle buttons.
