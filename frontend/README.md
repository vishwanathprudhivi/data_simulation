# simcore frontend

React + TypeScript (Vite) SPA: navigation, central canvas (scenario editor, run
monitor, comparison, results canvas, facility viewer, domain explorer) and the chat
panel. It is only a client of `/api/v1`; nothing it shows is computed here, and no
metric, unit or format is hard-coded (everything comes from the catalog API).

```bash
make api                 # API on :8000 (examples seeded)
make frontend            # dev server on :5173, proxies /api
make e2e                 # build + Playwright AC-UI-01/02 against the API serving dist/
```

`src/generated/` is generated from `../schemas` by `npm run gen:types`; never edit it
by hand. CI fails if it is out of date (`make frontend-types-check`).
