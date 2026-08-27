---
name: maps
display_name: Maps and Places
description: Research places, routes, and geographic facts through whichever maps/places toolkit the organization has connected, with current evidence and stated assumptions.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: connector
platform_key: connector.maps
activation: {mode: optional}
required_capabilities: {all_of: [connectors.composio]}
steps: [{order: 1, name: Resolve, description: Disambiguate the place name and geographic scope before querying anything.}, {order: 2, name: Compare, description: Fetch current route/place/hours data rather than relying on memory.}, {order: 3, name: Answer, description: State assumptions, mode of travel, and how fresh the source data is.}]
warnings: ["Place names are frequently ambiguous (a street name, a chain location, a landmark shared across cities) — resolve to a specific geocoded result before using it in a route or nearby search, and say which one you picked if it wasn't obvious.", "Opening hours, phone numbers, and similar POI metadata returned by a maps connector can be stale or user-contributed depending on the underlying provider — cross-check with a web search for anything time-sensitive (\"is it open right now\") rather than presenting connector data as guaranteed-current.", "Travel time estimates assume typical conditions unless the action explicitly supports live traffic — don't state a duration as if it accounts for current traffic unless you've confirmed the action actually returns that.", "Coordinates alone (latitude/longitude) are unambiguous; place names are not — when a user gives you both, prefer the coordinates for the actual query and use the name only for confirmation.", "Route/geocoding coverage and data quality vary by region and provider — a low-confidence or empty result in a sparsely-mapped area is a data-coverage gap, not necessarily a wrong query."]
---
Toda has no bundled geocoding client and no free-standing script for this — every maps/places lookup (geocoding, reverse geocoding, nearby search, directions, distance/travel time, timezone-for-location) runs as a Composio action through `integration_execute`, against whichever maps/places toolkit the organization has connected (commonly a Google Maps-family toolkit, but treat the exact provider as whatever `integration_search` actually surfaces for this organization rather than assuming one specific brand).

## Finding the right action

Always discover rather than guess a slug — different connected providers expose different action names for the same concept:

1. `integration_search` with a plain-English `use_case` — e.g. "geocode a place name to coordinates", "reverse geocode coordinates to an address", "find nearby restaurants around a location", "get driving directions between two places", "get travel distance and duration between two addresses", "get the timezone for a set of coordinates". It returns the matching action(s) for whatever provider this organization actually has connected, plus known pitfalls for that action.
2. `integration_describe` on the returned slug(s) — pay attention to whether the action wants a free-text address, structured coordinates, or both, and what travel mode / radius / category parameters it supports.
3. `integration_execute` with `{tool_slug, arguments}`, passing `connected_account_id` explicitly if more than one maps-family account is connected.

If nothing maps-related is connected for the organization at all, say so plainly rather than fabricating a plausible-sounding place lookup — a location question with no connected provider is a "this needs to be connected first" answer, not a best-effort guess from general knowledge.

## Workflow

### 1. Resolve

- Disambiguate before you query. "Springfield," "Main Street," and a chain restaurant name are all frequently ambiguous without a city/region qualifier — if the user's message doesn't pin it down and you can't infer it confidently from context (e.g., a shared location pin's coordinates, or an earlier message establishing the city), ask rather than picking arbitrarily.
- If the user supplied literal coordinates (e.g., from a shared location), use those directly for the query — coordinates are unambiguous where place names are not — and only use the resolved address/name for presenting the answer back to them.
- Note the intended mode of travel (driving/walking/cycling/transit) up front if the question involves a route or duration; don't default silently to driving when the context suggests otherwise (e.g., a question that asks how far a place is on foot).

### 2. Compare

- Fetch current data rather than answering from general knowledge — a maps connector call is cheap and gives you an actual, sourced answer instead of a plausible-sounding one that might be outdated (business closures, renamed streets, new routes).
- For "what's nearby" questions, prefer a nearby/places-search action with an explicit category over a generic text search when the discovered action supports categories — it produces more precise, comparable results (e.g., filtering to "restaurant" instead of hoping a free-text search happens to return restaurants).
- For distance/route questions, request the specific mode that matches the question and, where the action supports it, note whether the duration reflects live conditions or a typical/average estimate.

### 3. Answer

- State which specific place you resolved a name to, if there was any real ambiguity, so the user can correct you if you picked the wrong one.
- State the mode of travel and, if relevant, that the duration is a typical estimate rather than live-traffic-aware, unless you've confirmed the underlying action actually returns live conditions.
- For time-sensitive claims ("is it open now"), either rely on hours data the connector explicitly returned with a timestamp/freshness signal, or say you can't confirm current hours and suggest checking directly — don't assert business hours with more confidence than the source data supports.
- Include enough of the resolved address/coordinates in your answer that the user could act on it directly (e.g., paste it into their own maps app) rather than only a vague place name.

## Gotchas specific to how Toda runs this

- **Provider-agnostic by design.** This skill doesn't hardcode a specific maps provider's action names because the organization's connected toolkit determines what's actually available — always run the discovery step above rather than assuming a particular provider's terminology or action family.
- **Approval gate.** All of these lookups are reads — they execute immediately with no approval pause. If a discovered action ever looks like it would modify something (e.g., saving a place to a list, adding a review) rather than just looking something up, treat that as a write and confirm with the user before calling it, since that's a different risk category than the read-only lookups this skill is built around.
- **Freshness varies by provider and field.** Coordinates and basic addresses are generally reliable; opening hours, phone numbers, and similar POI metadata quality depends on how well the underlying provider's data is maintained for that specific place — treat those fields as "best available," not guaranteed-current, especially outside major metro areas.
- **Coverage gaps are data gaps, not bugs.** An empty or low-quality result in a rural or sparsely-mapped region reflects the underlying data source's coverage, not a malformed query — say so rather than retrying the same query repeatedly expecting a different result.
