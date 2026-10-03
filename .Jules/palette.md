## 2024-06-25 - Focus Management in Empty State CTAs
**Learning:** When users click on calls-to-action in empty states (like "Clear Search", "Return to All", or "Request New Story"), their keyboard focus is lost or reset to the top of the document if the CTA dynamically removes the empty state or scrolls the page.
**Action:** When a CTA scrolls to a new section or modifies the DOM to show new interactive elements, programmatically focus the target element (e.g. `element.focus({preventScroll: true})`) and ensure containers receive `tabindex="-1"` and `outline: none` so keyboard users have a continuous flow.

## 2024-06-25 - Dynamic ARIA States on Toggle Buttons
**Learning:** Icon-only toggle buttons (like theme switches or expand/collapse logs) can be confusing for screen readers if only the icon changes visually, while the `aria-label` and `title` remain static.
**Action:** Always dynamically update `title` and `aria-label` attributes alongside visual changes in toggle buttons to reflect both their current state and the action they will perform when activated.
