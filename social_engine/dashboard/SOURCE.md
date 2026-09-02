# FLOW dashboard dist (vendored)

Downloaded from public `erikhinla/flow-as` (no clone). Served by the social engine at `/` and `/flow-control`.

- https://raw.githubusercontent.com/erikhinla/flow-as/main/services/dashboard/dist/index.html
- https://raw.githubusercontent.com/erikhinla/flow-as/main/services/dashboard/dist/assets/index-9dd493e3.js
- https://raw.githubusercontent.com/erikhinla/flow-as/main/services/dashboard/dist/assets/index-ef66ae83.css

Local patches:
- `index.html` loads Tailwind Play CDN because the dist CSS is uncompiled `@tailwind` directives.
- JS approve panel also shows when `status === "review_required"` so alpha social creative review can be approved (upstream UI gated on `owner_role === "gamma"`).
- Dashboard fetch is same-origin `/api/flow` (nginx used to rewrite `/api` → bizbrain `/v1`).
