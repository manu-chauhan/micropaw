# MicroPaw Skills — Multi-Step Workflow Hints

## Shopping & Price Research
When user asks for deals, prices, or product comparisons:
1. Use delegate to search multiple sources in parallel:
   - delegate "Search Amazon for [product], browse top result, extract prices and ratings"
   - delegate "Search Best Buy/Walmart for [product], browse top result, extract prices"
   - delegate "Search for [product] reviews and recommendations"
2. Compile a comparison: product, source, price, rating, link
3. If user has location, also check "[product] near [location]" for local options

## Local Services ("near me")
When user asks for restaurants, food, services, or anything local:
1. Use delegate to search multiple sources in parallel:
   - delegate "Search [query] near [user location], browse top result for menu/hours/prices"
   - delegate "Search [query] near [user location] reviews, browse top result for ratings"
2. Compile results: name, rating, distance/area, price range, key details
3. Offer to dig deeper into any specific result

## Research & Deep Dive
When user asks to research a topic thoroughly:
1. Use delegate to spawn 2-3 sub-agents searching different angles in parallel
2. Each sub-agent: web_search → web_browse top result → extract key facts
3. Combine findings, cite sources, highlight consensus and disagreements

## Email Workflow
When sending email to a person by name:
1. Search contacts first to find their email address
2. If multiple matches or emails, ask user which one
3. Then compose and send the email

## Calendar Workflow
When creating events:
1. Check calendar for conflicts on the proposed date/time first
2. If conflict found, suggest alternatives
3. Then create the event

## Document Q&A
When user reads a file, PDF, or YouTube link:
- The content is auto-indexed for RAG — no extra steps needed
- When asked about it later (even from another channel), relevant chunks are auto-retrieved
- Reference specific details from the retrieved context, don't guess

## Web Browsing Strategy
When exploring a website interactively:
1. browse the URL first to get page content
2. Use scroll if the page is long — important content may be below the fold
3. Use click to follow links or buttons for more detail
4. Use screenshot when visual layout matters (menus, charts, maps)
5. Sessions persist within a turn — no need to re-browse the same page
