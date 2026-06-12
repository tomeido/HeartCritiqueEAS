## 2024-05-18 - Keyboard Accessibility for Interactive Divs
**Learning:** Interactive elements constructed using `<div>` (like `.list-item`) often lack implicit keyboard support and semantic meaning, rendering them invisible or inaccessible to screen readers and keyboard-only users.
**Action:** Always ensure that interactive `div` elements receive `tabIndex="0"`, a proper ARIA `role="button"` (or equivalent), and key event listeners (`Enter` and `Space`) to replicate native `<button>` behavior. Alternatively, use native `<button>` or `<a>` elements where possible.
