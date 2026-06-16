## 2024-05-18 - Preserve Accessible Names in Loading Spinners
**Learning:** Replacing a button entirely with a bare spinner (`<span class="spin"></span>`) during async operations strips its accessible name, leaving screen reader users unaware of the button purpose while waiting.
**Action:** Always append descriptive text to the spinner, hide the spinner element (`aria-hidden="true"`), and use `aria-busy="true"` on the parent button when entering a loading state.
