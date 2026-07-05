## 2024-05-18 - Restore Keyboard Focus on Story Close
**Learning:** When managing focus restoration on dynamically re-rendered elements (like story lists) in a vanilla HTML/JS SPA, relying on direct DOM node references can be brittle. If the list re-renders (e.g., due to background polling or filter changes) while a detail view is open, the stored reference becomes detached, breaking the focus return.
**Action:** Always rely on `data-id` attributes and `document.querySelector` to find and restore focus to the fresh DOM element representing the item.
