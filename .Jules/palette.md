## 2024-05-23 - Screen Reader Accessibility in Loading States
**Learning:** Replacing a button's innerHTML entirely with a purely visual spinner (`<span class="spin"></span>`) removes its accessible name, leaving screen readers with no context when the button is focused while disabled (e.g., during an async action). In addition, visual decorative elements like spinners should be explicitly hidden (`aria-hidden="true"`) to prevent screen readers from attempting to read them.
**Action:** Always retain or add context text (e.g., "투표 중...", "재검사 중...") alongside a loading spinner inside a button, and ensure decorative visual icons/spinners have `aria-hidden="true"`.

## 2024-05-23 - Empty State Guidance
**Learning:** Empty states without actionable guidance can leave new users confused about the next steps.
**Action:** Enhance empty states (especially the default 'all' view) with a helpful Call-to-Action (CTA) that points users to the main interaction point (e.g., the AI hunter invocation button).