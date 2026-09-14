## 2024-05-19 - Restoring Focus to Dynamically Re-rendered Elements
**Learning:** In a single-page application where list items trigger detail views and the list might re-render while the detail is open, storing DOM node references to restore focus upon closing the detail view is fragile. Nodes may detach, causing focus restoration to fail silently.
**Action:** Always attach a stable, unique `data-id` attribute to list items and use `document.querySelector` with that ID to query for the fresh DOM node and call `.focus({ preventScroll: true })` on it when the detail view closes.

## 2025-02-24 - Inline Loading States and Keyboard Discoverability
**Learning:** Adding a spinner to the search icon itself during an API call provides clear, immediate feedback without disrupting the layout. Additionally, indicating keyboard shortcuts in placeholders or tooltips (e.g. `/` for search and `Esc` for closing) improves discoverability for keyboard users.
**Action:** Always consider using existing nearby icons for inline loading states rather than creating new spinner elements that may cause layout shifts. Provide keyboard shortcut hints in UI text whenever implementing global keyboard event listeners.

## 2025-02-25 - Dynamic ARIA Labels for Toggle Buttons
**Learning:** Icon-only toggle buttons (like expanding/collapsing details) must dynamically update their `aria-label` and `title` to accurately reflect their current state, otherwise screen reader users and those relying on tooltips receive stale information about the action they are about to perform.
**Action:** Always update both `aria-label` and `title` attributes in the JavaScript toggle event handler, ensuring they accurately describe the action that will happen if the button is clicked again (e.g., changing from "View Details" to "Close Details").

## 2025-02-25 - Focus Management in Empty State CTAs
**Learning:** When empty state views contain Call-To-Action (CTA) buttons that clear filters, reset searches, or scroll to a different section, clicking them usually causes a major DOM re-render or layout shift. If focus is not explicitly managed, keyboard users lose their place and focus drops to the document body.
**Action:** Always programmatically call `.focus()` on the logical next interactive element (e.g., the search input, the "all" filter chip, or the main action button) in the onClick handler of empty state CTAs.
