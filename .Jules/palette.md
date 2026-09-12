## 2024-05-19 - Restoring Focus to Dynamically Re-rendered Elements
**Learning:** In a single-page application where list items trigger detail views and the list might re-render while the detail is open, storing DOM node references to restore focus upon closing the detail view is fragile. Nodes may detach, causing focus restoration to fail silently.
**Action:** Always attach a stable, unique `data-id` attribute to list items and use `document.querySelector` with that ID to query for the fresh DOM node and call `.focus({ preventScroll: true })` on it when the detail view closes.

## 2025-02-24 - Inline Loading States and Keyboard Discoverability
**Learning:** Adding a spinner to the search icon itself during an API call provides clear, immediate feedback without disrupting the layout. Additionally, indicating keyboard shortcuts in placeholders or tooltips (e.g. `/` for search and `Esc` for closing) improves discoverability for keyboard users.
**Action:** Always consider using existing nearby icons for inline loading states rather than creating new spinner elements that may cause layout shifts. Provide keyboard shortcut hints in UI text whenever implementing global keyboard event listeners.

## 2024-05-18 - Keyboard Focus Routing and Button Accessibility
**Learning:** Adding programmatic focus (`tabindex="-1"`, `outline: none;`) to target containers like `.gen-card` when scrolling to them ensures that users navigating via keyboard don't lose focus or have their focus reset to the `body` when DOM elements are updated/destroyed (such as empty states). Additionally, providing `title` attributes on disabled buttons, and ensuring dynamic toast updates (like a copy to clipboard success message), significantly improves the user's awareness of system state.
**Action:** When creating empty states with a call-to-action that involves scrolling to a form/section, always use `scrollFocus(element)` rather than just `scrollIntoView()`. Similarly, always provide informative tooltips (`title`) on buttons that are disabled dynamically so the user understands why the interaction is unavailable.
