## 2024-05-18 - Focus restoration on story close
**Learning:** When managing focus restoration on dynamically re-rendered elements (like story lists) in this SPA, storing direct DOM node references fails because they may become detached due to background polling.
**Action:** Rely on `data-id` attributes and `document.querySelector` to find and focus the correct element after closing the story.
