## 2026-06-10 - Interactive List Items Keyboard Accessibility
**Learning:** Custom interactive elements (like story list cards using `div`) frequently miss keyboard events and focus states, blocking keyboard-only navigation.
**Action:** Always add `tabindex="0"`, `role="button"`, `onkeydown` (handling Enter/Space), and `:focus-visible` styles to any custom `div` that has an `onclick` handler.
