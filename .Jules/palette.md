## 2024-06-20 - Adding explicit aria-controls to load-more buttons
**Learning:** For dynamic content loading where a button appends new elements to a container, adding `aria-controls="container-id"` helps screen readers understand the relationship and expect the region to change. This is critical for seamless accessible UX for continuous feeds.
**Action:** Always add `aria-controls` to "Load More" style buttons that append to a feed, pointing to the ID of the container being populated.
