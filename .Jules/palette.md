## 2024-05-19 - Restoring Focus to Dynamically Re-rendered Elements
**Learning:** In a single-page application where list items trigger detail views and the list might re-render while the detail is open, storing DOM node references to restore focus upon closing the detail view is fragile. Nodes may detach, causing focus restoration to fail silently.
**Action:** Always attach a stable, unique `data-id` attribute to list items and use `document.querySelector` with that ID to query for the fresh DOM node and call `.focus({ preventScroll: true })` on it when the detail view closes.

## 2025-02-24 - Inline Loading States and Keyboard Discoverability
**Learning:** Adding a spinner to the search icon itself during an API call provides clear, immediate feedback without disrupting the layout. Additionally, indicating keyboard shortcuts in placeholders or tooltips (e.g. `/` for search and `Esc` for closing) improves discoverability for keyboard users.
**Action:** Always consider using existing nearby icons for inline loading states rather than creating new spinner elements that may cause layout shifts. Provide keyboard shortcut hints in UI text whenever implementing global keyboard event listeners.

## 2026-09-16 - Focus management for empty state CTAs
**Learning:** When a button triggers an action that replaces or destroys its own container (like clicking a CTA in an empty state), keyboard focus is lost and resets to the document body, causing a poor accessibility experience.
**Action:** When creating empty state CTAs that trigger re-renders, explicitly chain a `.focus({preventScroll: true})` call to focus the next logical element (e.g., the reset filter chip, the search bar) to maintain context for screen reader and keyboard users.
