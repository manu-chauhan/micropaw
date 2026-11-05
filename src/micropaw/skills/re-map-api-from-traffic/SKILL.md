---
name: re-map-api-from-traffic
description: Turn a HAR file (recorded browser traffic) into an endpoint map and a small client sketch.
---

# Map an API from recorded traffic

Only use traffic from services the user is allowed to use this way.

1. Ask the user to record a HAR in the browser dev tools (Network tab, Save all as HAR) and save it in the workspace.
2. Run the `har` tool on the file. It lists endpoints by method, host and path with status codes, content types and query parameter names, and sketches an OpenAPI file and a client. Secrets and header values are left out on purpose.
3. Explain the flow in plain words: which call logs in, which calls fetch data, which parameters change between calls.
4. If the user wants code, write a small client in the workspace with file_write. Ask the user to supply any token from their own session, never invent one.
5. Do not call the live service to test it unless the user asks.
