## 2024-05-19 - Restoring Focus to Dynamically Re-rendered Elements
**Learning:** In a single-page application where list items trigger detail views and the list might re-render while the detail is open, storing DOM node references to restore focus upon closing the detail view is fragile. Nodes may detach, causing focus restoration to fail silently.
**Action:** Always attach a stable, unique `data-id` attribute to list items and use `document.querySelector` with that ID to query for the fresh DOM node and call `.focus({ preventScroll: true })` on it when the detail view closes.

## 2025-02-24 - Inline Loading States and Keyboard Discoverability
**Learning:** Adding a spinner to the search icon itself during an API call provides clear, immediate feedback without disrupting the layout. Additionally, indicating keyboard shortcuts in placeholders or tooltips (e.g. `/` for search and `Esc` for closing) improves discoverability for keyboard users.
**Action:** Always consider using existing nearby icons for inline loading states rather than creating new spinner elements that may cause layout shifts. Provide keyboard shortcut hints in UI text whenever implementing global keyboard event listeners.

## 2025-02-25 - Dynamic ARIA Labels and Focus Management for CTAs
**Learning:** Icon-only toggle buttons need dynamic `aria-label` and `title` attributes that update with their state (e.g., "검사 이력 닫기" vs "검사 이력 보기") to provide clear context for screen reader users. Additionally, when a CTA button triggers a list re-render (like clearing filters in an empty state), the button itself is destroyed, leading to lost focus.
**Action:** Always programmatically manage focus (using `element.focus({preventScroll: true})` inside a `setTimeout` if necessary) to shift focus to a logical next element (like the "All" filter chip or action button) when the clicked element is about to be unmounted. Ensure icon-only buttons update their ARIA labels dynamically via JavaScript on state changes.
