## 2024-05-18 - Interactive Lists Need Keyboard Support
**Learning:** Attaching `onclick` to non-interactive elements like `div` for a list of items breaks keyboard navigation for screen readers and power users.
**Action:** When making a `div` or `li` interactive, always add `role="button"`, `tabindex="0"`, and a `keydown` handler for "Enter" and "Space" keys to trigger the action, along with appropriate `focus-visible` styling.
