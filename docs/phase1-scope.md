# Phase 1 — Proposed MVP Scope

**Status: MVP implemented following the user's request to start Phase 1. Local live pilot pending.**

## Context

Guitar Radar aims to follow guitar-related Facebook groups, collect posts, identify reposts across groups, and present one clean feed. Phase 0.5 completed a live validation of the collectors and reported `READY FOR PHASE 1` for the observed sample. `facebook-go` is the primary candidate; `fbgql` remains useful for comparison and audit. The benchmark is a bounded sample, not a production reliability guarantee.

This document records the MVP scope. Operational commands and the selected defaults are in [phase1-running.md](phase1-running.md).

## Product outcome

An owner can configure guitar groups and browse recent, collected posts from one place. Repeated polls must not create duplicate database rows. Exact repost candidates should be easy to review while uncertain matches remain separate until a person confirms them.

## Recommended MVP

### Group setup

- Start with `vegmpremium` as the pilot group.
- Store groups as configuration records so the design can support multiple groups without a schema change.
- Show each group’s enabled state, last successful collection, and most recent error.
- Let the owner trigger a manual collection for a group.

### Collection

- Use `facebook-go` as the normal collector.
- Keep `fbgql` as an optional comparison/audit tool; do not make routine feed availability depend on it.
- Collect posts and available media metadata from each enabled group, using a bounded lookback so old posts are not reinserted on every run.
- Run one collection at a time for the account. Use a configurable interval after the manual collection flow is stable; do not add a distributed queue in this MVP.
- Treat login expiry, checkpoint, account restriction, and rate limiting as a paused state that needs manual attention. Do not automate login or bypass Facebook controls.
- Preserve the source post URL and source timestamps. Track `first_seen_at` separately from the time the post was published.

### Storage and repeat safety

Proposed database: PostgreSQL. Keep the collector output format as an ingestion boundary so collector changes do not require redesigning the feed.

Minimum records:

- **Groups:** platform group ID, configured URL/alias, display label, enabled state, and collection health.
- **Posts:** group, platform post ID, canonical permalink, text, author fields when supplied, published time, first/last seen times, and source/update timestamps.
- **Media:** post, media URL, type evidence, dimensions when available, and display order. Do not download or rehost media in the MVP.
- **Collection runs:** start/end time, group, status, counts, and safe error code.

Enforce uniqueness by `(platform, group_id, post_id)`. Repeated collection should update the existing post’s current fields and last-seen time while preserving its first-seen time. A missing post in a later feed response must not automatically delete it.

### Clean feed

- Show newest posts first with text, group name, published time, available image previews, and a link back to Facebook.
- Support filtering by group, date range, and whether images are present.
- Provide simple text search over collected post text.
- Show the source group and post link on every item so users can verify the original.
- Show a small collection-health view with last success and current paused/error state.

### Duplicate handling

- Automatically collapse repeated ingestion of the same Facebook post ID into one post record.
- Treat identical canonical permalinks as the same source post.
- In this MVP, show cross-group reposts as **duplicate candidates** only when a deterministic comparison finds an exact normalized text match. Keep the original posts and their group links visible.
- Require manual confirmation before merging distinct post IDs into one listing. Do not use AI or fuzzy/semantic matching in the first slice.

## Proposed technical shape

- A private web application with a small API and a single collection worker.
- PostgreSQL for durable data and collection history.
- One sequential worker that invokes the validated collector and stores normalized results transactionally.
- A basic browser UI for the owner; protect it with authentication or keep it behind a private network boundary before exposing it beyond the developer machine.
- Configuration and Facebook session material stay outside source control. Never log cookies, session tokens, or authorization headers.
- Keep raw/debug data disabled by default in application operation. If enabled for diagnosis, sanitize it and apply short retention.

Selected implementation: Python FastAPI reusing the validated normalizer, a static HTML/CSS/JavaScript frontend, PostgreSQL 16, and an integrated sequential worker (or a separate worker process). Owner access uses generated HTTP Basic credentials on loopback by default. Remote hosting requires HTTPS/private networking. There is no Redis or distributed queue.

## Out of scope for the first MVP

- AI, guitar/model parsing, price extraction, deal scoring, recommendations, and alerts.
- Automatic fuzzy or semantic deduplication and automatic merging of distinct Facebook posts.
- Comments, replies, group member profiles, and data beyond the group feed posts needed for the clean feed.
- Downloading, mirroring, or rehosting Facebook photos.
- Public multi-tenant accounts, billing, mobile apps, and production-scale analytics.
- Automated Facebook login, checkpoint handling, or other access-control bypasses.

## Acceptance criteria

1. The owner can configure the pilot group and run a collection manually.
2. A successful run stores normalized posts and media metadata with the original group/post link and published time when available.
3. Running the same collection again does not create duplicate rows for the same group and post ID.
4. A failed or interrupted run does not erase previously stored posts and records a safe status/error code.
5. Authentication or checkpoint errors pause future collection until the owner resolves them manually.
6. The feed supports newest-first browsing, group/date/image filters, and text search.
7. Exact duplicate candidates retain links to every source post; the system never silently merges distinct post IDs.
8. The app can be started and operated from documented commands, and database backup/restore steps are documented.
9. Tests cover idempotent ingestion, failed-run preservation, feed filters, and duplicate-candidate behavior without requiring Facebook credentials.

## Delivery sequence

1. Use the recommended private/local defaults below for this MVP.
2. Implement one vertical slice: one configured group, manual collection, PostgreSQL persistence, and a basic newest-first feed.
3. Add repeat-safe scheduled collection and collection-health visibility.
4. Add conservative duplicate candidates and manual confirmation.
5. Pilot on real collections, review privacy/retention, and only then expand to more groups.

## Selected baseline and possible future alternatives

The recommended column is the implemented baseline. Alternatives require a subsequent change.

| Decision | Recommended starting point | Alternative to decide |
|---|---|---|
| Initial rollout | One pilot group; schema supports multiple | Configure several groups on day one |
| Who can use the app? | Owner-only, private network | Multiple authenticated users |
| Where does it run? | Private machine/server controlled by the owner | Cloud/VPS deployment |
| Collection cadence | Manual first, then a conservative configurable interval | Scheduled collection immediately |
| Database | PostgreSQL | SQLite for a local-only prototype |
| Media storage | Keep metadata/URLs and link to Facebook | Download/cache images, subject to rights and retention decisions |
| Cross-post behavior | Candidate plus manual confirmation | Automatic grouping under stricter matching rules |

Before wider deployment, decide the hosting/private-access boundary, retention requirements, and any broader multi-user or automatic cross-post behavior. These are outside the local owner-only MVP.
