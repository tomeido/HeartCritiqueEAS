## 2024-05-19 - Restoring Focus to Dynamically Re-rendered Elements
**Learning:** In a single-page application where list items trigger detail views and the list might re-render while the detail is open, storing DOM node references to restore focus upon closing the detail view is fragile. Nodes may detach, causing focus restoration to fail silently.
**Action:** Always attach a stable, unique `data-id` attribute to list items and use `document.querySelector` with that ID to query for the fresh DOM node and call `.focus({ preventScroll: true })` on it when the detail view closes.

## 2025-02-24 - Inline Loading States and Keyboard Discoverability
**Learning:** Adding a spinner to the search icon itself during an API call provides clear, immediate feedback without disrupting the layout. Additionally, indicating keyboard shortcuts in placeholders or tooltips (e.g. `/` for search and `Esc` for closing) improves discoverability for keyboard users.
**Action:** Always consider using existing nearby icons for inline loading states rather than creating new spinner elements that may cause layout shifts. Provide keyboard shortcut hints in UI text whenever implementing global keyboard event listeners.

## 2024-05-19 - Empty State Focus Management and Dynamic ARIA Labels
**Learning:** For empty states with call-to-action buttons (like resetting search or returning to a full list), navigating via keyboard can leave focus stranded if the active element is removed or the state changes drastically. Furthermore, icon-only buttons that change function (like expand/collapse) without updating their accessible names create confusion for screen reader users.
**Action:** When an empty state CTA causes a layout shift or list regeneration, always programmatically move focus to the newly revealed logical container (using `scrollFocus` or `.focus({preventScroll: true})` on an element with `tabindex="-1"`). For toggleable icon buttons, always dynamically update both `aria-expanded` and the accessible name (`aria-label`/`title`).
