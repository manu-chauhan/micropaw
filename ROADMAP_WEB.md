# Web Capabilities Roadmap

## Current State
- `web_search` — SearXNG (public instances) → DuckDuckGo fallback, or Brave with API key
- `web_browse` — Playwright headless Chrome, read-only text/HTML extraction + YouTube transcript
- **No interaction**: can't click, type, fill forms, scroll
- **No location awareness**: "near me" depends on search backend IP geolocation
- **No screenshots**: can't show visual page content to multimodal LLMs
- **No auth/sessions**: fresh browser context each call, no login persistence

## Phase 1 — In Progress

### 1. Location Context
- Add `location` field to config (city, zip, country)
- Inject into system prompt so agent appends location to "near me" queries
- Also readable from `~/.micropaw/USER.md` if user prefers prose
- **Zero new tools** — just smarter prompting

### 2. Screenshots
- Add `screenshot` extract mode to `web_browse`
- Playwright `page.screenshot()` → return as base64 image
- Multimodal providers (Claude, GPT-4o, Gemini) can "see" the page
- Useful for: visual content, layouts, verifying page state

### 3. Page Interaction
- Add `interact` action to `web_browse` with sub-actions:
  - `click` — click element by CSS selector or text
  - `type` — type text into an input field
  - `scroll` — scroll down/up
  - `select` — select dropdown option
- Session persistence within a single agent turn (keep page context alive)
- Enables: search boxes, pagination, expanding sections, form filling

## Phase 2 — Future

### 4. Session Persistence Across Turns
- Keep browser context alive across multiple tool calls (cookies, login state)
- Agent can log into sites, maintain cart state, etc.

### 5. Structured Extraction
- `web_extract` tool for pulling structured data (product info, menus, prices)
- CSS selector-based or LLM-based extraction from page HTML

### 6. Multi-Step Browser Agent
- "Computer use" style loop: browse → screenshot → decide → act → repeat
- Agent drives browser autonomously to achieve a goal
- Example: "order me a pizza from Domino's"

### 7. Direct API Integrations
- OAuth/API access to services (Uber Eats, DoorDash, Amazon)
- More reliable than browser automation but per-service effort
