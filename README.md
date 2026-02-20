# Simple Docs

This repository contains the Simple documentation site, built with **Docusaurus**.

### What you edit
Most changes are made by editing Markdown files:
- `docs/` — documentation pages (`.md` / `.mdx`)
- `static/` — images and files served as-is (e.g. `static/img/...`)
- `sidebars.js` — sidebar/navigation ordering
- `docusaurus.config.js` — site config (navbar, footer, title, etc.)

## Quick Start

### Prerequisites
- Node.js **20 LTS** recommended
- npm installed (comes with Node)

Verify:
```bash
node -v
npm -v
```

### Install dependencies
From inside Website/ run:
```bash
npm ci
```

### Run the site locally (dev server)
```bash
npm run start
```

Open: `http://localhost:3000`

This run a development server with hot reload:
- edit a file in docs/
- save
- browse refreshes automatically

## How to add or update docs

### Add a new page
1. Create a new file in `docs/`, for example: `docs/onboarding.md`
2. Add font-matter at the top (recommended):
```Markdown
---
title: Onboarding
sidebar_position: 2
---
```
3. Add the page to the sidebar (`sidebars.js`) if needed,

### Add images
Put images under:
- `static/img/`
Then reference them in docs like:
```Markdown
![Diagram](/img/diagram.png)
```

### Sidebar / Navigation
The sidebar is controlled by `sidebars.js`.
Typical workflow:
- Add the doc file to `docs/`
- Add its entry to the sidebar array in `sidebars.js`
- Preview locally with `npm run start`

