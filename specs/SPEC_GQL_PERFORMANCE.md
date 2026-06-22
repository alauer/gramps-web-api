# SPEC: GQL Performance — Making Gramps Web API Usable for 50K+ Person Trees

**Status**: Draft
**Target repo**: gramps-web-api (your fork: `alauer/gramps-web-api`)
**Source investigation**: [`MCP_PERFORMANCE_INVESTIGATION.md`](../MCP_PERFORMANCE_INVESTIGATION.md)
**Spec author**: derived from the investigation + live measurements
**Date**: 2026-06-17

---

## 1. Problem statement

`/api/people/?gql=...` (and by extension every list endpoint with GQL/OQL/filter
arguments) is unusably slow for the user's real workload:

| Test | Cold | Warm |
|---|---|---|
| Simple page, no GQL, no extend | **14.69s** | **0.06s** |
| GQL filter (`first_name ~ "John"`) | **>120s** (timeout) | n/a |
| GQL + `extend=all` | >30s (worker-starved) | n/a |

Source: live measurements against the production deployment at
`https://gramps.thelauerfamily.us/api` (53,170 persons in the `gramps` tree).

The bottleneck is **not** the database. The `SELECT handle, json_data FROM person`
query completes in ~1 second. The remaining time is **Python work in the
gunicorn worker**: deserializing 53,170 Person objects into memory (~270-450 MB),
running the GQL filter, sorting, paginating, and JSON-serializing the response.

This makes the API unusable for the user's single-user / sporadic-usage pattern
(50K-person tree, occasional browsing, an MCP client that times out at 30s).

### Evidence

From `pg_stat_activity` captured during a slow GQL request:

```
07:32:53 — pid=3280 active: SELECT handle, json_data FROM person
07:32:54 — (only one sample of this query in 120s of polling)
```

The single SQL round trip is fast. The remaining 119 seconds of request time
is Python work.

From the local Python benchmark (NS proxy for `gramps.gen.lib.Person`):

```
fetch 53K json_data blobs:        0.05s
json.loads on 53K blobs:          1.72s
dict → NS conversion 53K:         2.38s
GQL filter (NS proxy):            0.041s (2,728 matches)
sort + paginate + json.dumps:     ~0.005s
TOTAL NS-proxy:                   ~4.2s
```

On the real server, the same workload takes >120s. The 30× gap is
`gramps.gen.lib.Person`'s heavy `__getattr__` proxying on every attribute
read (which `gramps_ql.match` triggers per-attribute per-object).

### What the investigation ruled out (re-confirmed)

- DB indexes are fine (`person_pkey`, `person_gramps_id`, `person_given_name`, `person_surname` — all btree)
- `pg_trgm` is unnecessary; the ILIKE scan on 53K rows is sub-3ms with `LIMIT 20`
- MCP server is not at fault — its 22 tests pass when the API responds quickly
- Wrong tree ID was a separate bug (already fixed by switching to `gramps` as the tree ID)

---

## 2. Goals

Concrete, measurable latency targets for the user's deployment (53K persons, 8-worker gunicorn, single user):

| Metric | Current | Target | Stretch |
|---|---|---|---|
| Cold simple page, no GQL | 14.69s | **< 2s** | < 1s |
| Cold GQL filter, paginated | >120s | **< 5s** | < 2s |
| Warm any page (cache hit) | 0.06s | **< 50ms** | < 20ms |
| Memory per worker under load | unbounded (potentially OOM) | **< 500MB** | < 300MB |
| Worker starvation (one cold blocks others) | observed | **none** | none |

### Non-goals (out of scope for this spec)

- **Push GQL to SQL** — the architectural fix. Requires changes to `gramps_ql`
  (no SQL backend today) and a refactor of the `get()` method's "load all,
  filter in Python" pattern. Effort: weeks-to-months. Tracked as a separate
  future spec.
- **Multi-server scaling** — the user's deployment is single-server. A future
  spec could address read replicas, sharding, or read-only mirrors.
- **Gramps library changes** — the bottleneck is largely in the Gramps
  library's Python attribute access. We're not modifying Gramps here.
- **1M+ person trees** — this spec targets the 50K-100K range. Beyond that,
  the architectural fix (push GQL to SQL) becomes unavoidable.
- **Changing the API contract** — the response shape, query parameter names,
  and HTTP status codes stay the same. Backward compatibility is mandatory.

---

## 3. User stories

- **S1**: As a single user with sporadic access to a 53K-person tree, I want
  my first request after returning to the app to complete in under 5 seconds,
  so I don't have to wait while my browser spins.
- **S2**: As a single user, I want subsequent identical requests to return in
  under 50ms, so navigating within the same view feels instant.
- **S3**: As a single user, I want to edit a person and then have the
  people-list view reflect the edit, without all other cached views being
  invalidated.
- **S4**: As an MCP client, I want the API to handle a GQL-filtered people
  query within 30 seconds (my client timeout), so the MCP test passes.
- **S5**: As a developer, I want to be able to manually pre-warm the cache
  for known query patterns, so the first user after a server restart sees
  warm responses.

---

## 4. Functional requirements

Each requirement has a unique ID, a clear change, an acceptance test, and a
phase. Phases are independently shippable.

### Phase 1 — Config-only, zero risk

#### F1.1: Switch `request_cache` to RedisCache

- **Where**: `gramps_webapi/config.py:55-60` (default), and per-deployment
  env vars `GRAMPSWEB_REQUEST_CACHE_CONFIG__*`
- **Change**: `CACHE_TYPE = "RedisCache"`, `CACHE_THRESHOLD = 0` (unlimited)
- **Why**: FileSystemCache is per-disk-pickle, slow on the read side, and
  per-worker (each gunicorn worker has its own disk cache, not shared).
  Redis is in-memory, shared across workers, and survives container restarts.
- **Acceptance**: After a cold request, the response is cached in Redis DB 2
  (verified via `redis-cli -n 2 KEYS '*'`). Subsequent identical requests
  return in <50ms. The cache survives a `docker restart` of the API container.
- **Already implemented**: this is the change that was just made to the
  user's prod deployment, verified by inspecting Redis keys.

### Phase 2 — Small code changes, low risk, modest wins

#### F2.1: Drop the second `get_*_handles(sort_handles=True, locale=locale)` call

- **Where**: `gramps_webapi/api/resources/base.py:595-598`
- **Change**: always call `query_method()` without the `sort_handles` and
  `locale` arguments; let the in-memory `objects = sorted(objects, key=...)`
  (line 600-602) use Python's stable handle order instead of locale-aware
  surname order.
- **Why**: This is the second of the two unbounded SQL queries mentioned in
  the investigation. The `ORDER BY surname COLLATE "en_US"` over 53K rows
  is ~1-3s on its own, and the user-facing difference between
  locale-surname-sorted and handle-sorted is invisible (the frontend
  re-sorts client-side anyway for many views).
- **Acceptance**: `EXPLAIN ANALYZE` of the handles query shows no `ORDER BY`
  clause. `pg_stat_activity` during a slow request shows at most one
  handles-related query (the `iter_*` query), not two. Cold latency on the
  simple page drops by 1-3 seconds.
- **Trade-off documented in commit message**: default sort is now handle-order,
  not locale-surname. The frontend can re-sort; this is a back-end default only.

#### F2.2: Fix N+1 in `people.py:object_extend`

- **Where**: `gramps_webapi/api/resources/people.py:60-78`
- **Change**: when `extend=all` (or per-flag equivalents) is requested,
  collect all family handles across the page, batch-fetch them with a
  single `get_families_from_handles(db_handle, list(handles))` call (or
  equivalent Gramps API), and distribute back to the `obj.extended`
  structures. Same for `parent_families` and `primary_parent_family`.
- **Why**: The current code does one `get_family_by_handle()` per family
  per person. For a 20-person page with ~30 family references, that's
  20 + 30 = 50 round-trips. This is the worst offender on `extend=all`
  callers (the MCP client is one).
- **Acceptance**: For a 20-person page with `extend=all`, the number of
  DB queries is bounded by the number of distinct object types, not the
  number of references. Specifically, `extend=all` should issue
  ≤ 5 queries (one per object type's bulk fetch), not 50+. The existing
  tests for `object_extend` still pass.

#### F2.3: Defensive object cap

- **Where**: `gramps_webapi/api/resources/base.py:587` (after the
  `list(iter_objects_method())` call)
- **Change**: if `len(objects) > MAX_OBJECTS` (default 100,000, configurable
  via `GRAMPSWEB_MAX_OBJECTS_PER_REQUEST`), abort with `413 Payload Too
  Large` and a clear message including the actual count and the cap.
- **Why**: The 53K-row request holds a worker hostage for 14-120s, including
  the second 270-450 MB allocation. A malicious or runaway request could
  load the entire DB into memory and OOM the worker. This is a safety
  guardrail, not a primary fix.
- **Acceptance**: A request that would load > 100,000 objects returns 413
  within 1s (the cap check happens before any further work). The 413
  response includes the cap value and the actual count. The default cap
  is large enough not to affect any normal use case (the largest single
  primary object type in the user's DB is 53,170 persons).

### Phase 3 — Medium-effort changes, medium risk, big wins

#### F3.1: Per-type cache invalidation using the `changes` table

- **Where**: `gramps_webapi/api/cache.py:78-89` (`make_cache_key_request`)
- **Change**: instead of a single `db_timestamp` in the cache key, use
  per-table timestamps derived from the `changes` table (which Gramps
  maintains per write). The cache key becomes
  `tree_id + min(changes.timestamp WHERE table=requested_table) + path + arg_hash + permission_hash`.
- **Why**: The current `db_timestamp` is `os.path.getmtime("meta_data.db")`,
  which changes on **any** DB write. So a person edit invalidates the
  `/api/events/`, `/api/sources/`, `/api/places/` caches too. With per-type
  invalidation, only the types that actually changed lose their cache.
- **Acceptance**: After editing a single person, `/api/events/` cache is
  still warm. After editing an event, `/api/people/` cache is still warm.
  The `changes` table is updated correctly by Gramps (no API change
  needed there — it's a built-in feature).
- **Trade-off**: slightly more complex cache key computation (one query
  per request to look up the relevant changes row). But that query is
  cheap (indexed by `(change_time, table_name)` if maintained; otherwise
  full table scan of `changes` which is small).

#### F3.2: Pre-warm endpoint

- **Where**: new file `gramps_webapi/api/resources/admin.py` (or
  appended to `gramps_webapi/api/resources/cache.py`)
- **Change**: `POST /api/admin/prewarm` with body
  `{"paths": ["/api/people/?page=1&pagesize=20", "/api/people/?page=2&pagesize=20", ...]}`
  iterates the paths and makes an internal HTTP request to each, populating
  the cache. Returns 202 with a list of paths and their HTTP status.
- **Why**: Allows the user (or a cron job) to populate the cache
  proactively, e.g., after a server restart or before a known work
  session. Solves S5.
- **Acceptance**: Endpoint requires `PERM_VIEW_SETTINGS` (Admin). A
  request to `/api/admin/prewarm` with 5 paths returns 202; subsequent
  requests to those 5 paths return in <50ms (warm cache). Errors
  in prewarm (e.g., DB unavailable) are reported in the response but
  don't fail the whole batch.

#### F3.3: Stale-while-revalidate decorator (optional)

- **Where**: `gramps_webapi/api/cache.py` (new function)
- **Change**: alternative decorator that returns the cached response
  even if `db_timestamp` has changed since caching, but kicks off a
  background task to refresh. The response includes an `X-Cache: stale`
  header. The refresh uses the same code path as a normal request but
  runs in a celery task.
- **Why**: Trades freshness for latency. For a single user on a
  sporadically-edited tree, "show me the slightly-stale list" is
  better than "make me wait 14 seconds". Solves S1 partially without
  fixing the underlying bottleneck.
- **Acceptance**: After a DB write, the next request to a cached
  endpoint returns the stale data immediately with `X-Cache: stale`
  header. A celery task is queued to refresh. The next request after
  the refresh completes returns the fresh data with `X-Cache: hit`
  header. If the refresh fails, the stale data is kept; subsequent
  requests continue to return it.
- **Trade-off**: requires celery worker, which the user already has
  running (`grampsweb_celery` container). Adds complexity; not
  required for the latency targets above. Mark as optional.

### Phase 4 — Out of scope (separate spec)

#### F4.1: Push GQL to SQL

- **Why out of scope**: Requires either (a) a SQL backend for `gramps_ql`
  (which doesn't exist), or (b) a hand-rolled GQL→SQL translator inside
  the API. The user is right that this is the architectural fix, but
  it's a multi-week project on its own.
- **What it would look like**: when `gql` is in the args, parse the
  GQL string, translate the `=` and `~` operators on indexed fields to
  a SQL `WHERE` clause, push that into a `iter_*(where=...)` call (which
  Gramps's Postgres backend supports), and only fetch the matching
  handles. Then the existing `get_*_handles` + sort + paginate
  pipeline runs on the small filtered set, not the full 53K.
- **Spec it later** when the GQL→SQL story is clearer (likely requires
  a proof-of-concept translator first).

---

## 5. Non-functional requirements

- **N1: Backward compatibility.** No changes to API contract, request
  schemas, response schemas, or HTTP status codes. The defensive object
  cap (F2.3) introduces a new 413 response, but only for requests that
  would have hung or OOMed before — a strict improvement.
- **N2: No new external dependencies.** RedisCache is the only new
  dependency, and the user already has Redis running for Celery.
  Per-type cache invalidation (F3.1) uses the existing `changes`
  table. Pre-warm (F3.2) uses the existing celery worker. No new
  pip packages, no new system services.
- **N3: Performance regression test.** A new pytest benchmark
  (`tests/perf/test_cold_path.py`) that:
  - Sets up a 50K-person SQLite test database
  - Times a cold simple page request
  - Times a cold GQL filter request
  - Asserts cold simple < 5s, cold GQL < 10s
  - This is the regression guard; the test passes when the spec is
    implemented and stays green on future changes
- **N4: Memory bound.** A worker handling a cold simple page must not
  exceed 500MB resident memory (the user's container has
  `gunicorn -w 8`, so 8 workers × 500MB = 4GB max, well within
  typical container limits). F2.3 enforces this for runaway requests.
- **N5: No cache stampede.** When a cache entry expires, the next
  request should not trigger N concurrent cold loads (which is what
  happens now when 8 workers all see a miss at once). F3.3 (stale-while-
  revalidate) addresses this. Without F3.3, the stampede is bounded
  by the number of distinct query URLs in the cache, not concurrent
  workers, so it's not a regression.

---

## 6. Design

### F2.1 — drop second handles query

Current code (`base.py:595-602`):
```python
if self.gramps_class_name in ["Event", "Repository", "Note"]:
    handles = query_method()
else:
    handles = query_method(sort_handles=True, locale=locale)
handle_index = {handle: index for index, handle in enumerate(handles)}
objects = sorted(objects, key=lambda obj: handle_index.get(obj.handle, len(handles) + 1))
```

New code:
```python
# always use the no-sort variant; default order is handle-insertion order.
# Trade-off: not locale-surname-sorted. Frontend re-sorts where it matters.
handles = query_method()
handle_index = {handle: index for index, handle in enumerate(handles)}
objects = sorted(objects, key=lambda obj: handle_index.get(obj.handle, len(handles) + 1))
```

Net change: 1 line (the `if/else` collapses to one call). The `in
["Event", "Repository", "Note"]` branch becomes dead code and is removed.

### F2.2 — batch family fetches

Current code (`people.py:60-78`):
```python
if "all" in args["extend"] or "family_list" in args["extend"]:
    obj.extended["families"] = [
        get_family_by_handle(db_handle, handle)
        for handle in obj.family_list
    ]
if "all" in args["extend"] or "parent_family_list" in args["extend"]:
    obj.extended["parent_families"] = [
        get_family_by_handle(db_handle, handle)
        for handle in obj.parent_family_list
    ]
```

This runs **per-person** as `object_extend` is called. So for 20 people,
that's 20 invocations × 3 lists = up to 60 individual `get_family_by_handle`
calls.

New code (pseudocode — the actual Gramps API for batch fetching needs
investigation):
```python
def batch_fetch_families(db_handle, handles: set[str]) -> dict[str, Family]:
    """Fetch many families in one query, return a handle->Family map."""
    # Gramps' DbGeneric has get_family_from_handle(handle) per call.
    # For a batch, we have two options:
    #   (a) Use the cursor API: get_family_cursor() iterates all families
    #       in one SELECT, then we build a handle->Family dict from that.
    #       - O(53K) one-time cost per request (bad)
    #   (b) Add a batched method to Gramps' DB layer (out of scope for
    #       this repo; would need an upstream PR)
    #   (c) Issue a single raw SELECT handle, json_data FROM family
    #       WHERE handle IN (...) and parse — but bypasses Gramps' serializer
    # For now: option (a) is acceptable because the family table is much
    # smaller than person. Reuse the cursor result across all 20 persons
    # on the page.
    return {f.handle: f for f in db_handle.get_family_cursor()}

# in object_extend:
if extend_includes_families(args):
    if not hasattr(self, '_family_cache'):
        needed = set()
        # we don't have the full list of persons here; do two passes:
        #   pass 1: collect all needed handles
        #   pass 2: fetch & distribute
    # ... etc
```

This is the most complex of the Phase 2 changes. Implementation needs
careful handling of when the family cache is built (once per request
across all `object_extend` calls, not per person). The actual mechanism
will be a class-level cache or a request-scoped cache on `g`.

### F2.3 — defensive object cap

New config:
```python
MAX_OBJECTS_PER_REQUEST = 100_000
```

New code in `base.py:587`:
```python
objects = list(iter_objects_method())
if len(objects) > current_app.config["MAX_OBJECTS_PER_REQUEST"]:
    abort_with_message(
        413,
        f"Query would return {len(objects)} objects, "
        f"exceeding the cap of {current_app.config['MAX_OBJECTS_PER_REQUEST']}. "
        f"Use a more specific filter or pagination."
    )
```

### F3.1 — per-type cache invalidation

New function in `cache.py`:
```python
def get_table_change_timestamp(tree_id: str, table_name: str) -> float:
    """Get the most recent change timestamp for a given table.

    Returns os.path.getmtime(meta_data.db) if the changes table is
    unavailable, for backward compatibility.
    """
    dbmgr = get_db_manager(tree_id)
    db = get_db_handle()
    try:
        row = db.curs.execute(
            "SELECT MAX(change_time) FROM change WHERE table_name = ?",
            (table_name,),
        ).fetchone()
        if row and row[0]:
            return float(row[0])
    except Exception:
        pass
    return get_db_last_change_timestamp(tree_id)
```

Modified cache key in `make_cache_key_request`:
```python
# determine which table this endpoint reads (e.g., 'person' for /api/people/)
table_name = path_to_table_name(request.path)  # /api/<plural>/... -> <singular>
table_ts = get_table_change_timestamp(tree_id, table_name)
cache_key = tree_id + str(table_ts) + request.path + arg_hash + permission_hash
```

The `path_to_table_name` function maps `/api/people/` → `person`, `/api/events/` → `event`, etc. Endpoints that span multiple tables (e.g., `/api/people/{handle}/timeline` which reads events) use the most recent of the relevant tables.

### F3.2 — pre-warm endpoint

New resource:
```python
class PrewarmResource(ProtectedResource):
    @api_blueprint.arguments(PrewarmBodyArgs, location="json")
    def post(self, args):
        require_permissions([PERM_VIEW_SETTINGS])
        results = []
        for path in args["paths"]:
            # make an internal request; flask test_client is the right tool
            with current_app.test_client() as client:
                resp = client.get(path, headers={"Authorization": f"Bearer {get_jwt()}"})
                results.append({"path": path, "status": resp.status_code})
        return {"results": results}, 202
```

This is a simple "hit each URL once" loop. The actual pre-warm happens as
a side effect of the test_client request going through the full
`@request_cache_decorator` machinery.

---

## 7. Phased implementation plan

The phases are independently shippable. Each ends with a measurable
improvement and a green test suite. The user can stop after any phase
and still benefit.

### Phase 1 — Config (1 hour of work, no code changes)

1. Document the `GRAMPSWEB_REQUEST_CACHE_CONFIG__*` env-var changes in
   `BOOTSTRAP.md` (already done in the dev environment).
2. Update `gramps_webapi/config.py:55-60` defaults to recommend RedisCache
   (with comment noting that the existing FileSystemCache is a fallback).
3. Update the docker-compose example / Dockerfile docs to suggest the
   Redis env vars.
4. **Done. No code change required for the user's deployment (already
   applied via env vars).**

**Expected improvement**: warm latency drops from 60ms to ~5-20ms;
cache shared across workers; survives container restarts. Cold latency
unchanged.

### Phase 2 — Small code changes (1-2 days of work)

1. **F2.1** (1-2 hours): one-line change in `base.py:595`. Easy to
   review, easy to revert. Single commit.
2. **F2.2** (4-6 hours): batch family fetch in `people.py:object_extend`.
   Touches the most-used endpoint. Needs careful testing with the
   existing test suite. Single commit.
3. **F2.3** (1 hour): defensive cap. One config option, ~5 lines in
   `base.py:587`. Single commit.
4. **N3 regression test** (2-3 hours): add the perf benchmark.
5. **Verification**: run the user's GQL test against the live API
   (after deploy). Cold simple page should drop from 14.69s to
   ~12s (small win from F2.1). Cold GQL should drop from >120s to
   ~80-100s (modest win from F2.1 + F2.2 only on GQL paths).

**Expected improvement**: cold simple page <12s; cold GQL <100s;
warm <50ms (already achieved in Phase 1); memory bounded by
F2.3 (no more OOM risk on runaway requests).

### Phase 3 — Medium changes (1-2 weeks of work)

1. **F3.1** (3-4 days): per-type cache invalidation. Touches
   `cache.py` only. Needs the `path_to_table_name` mapping table.
   Need to verify Gramps' `change` table is populated as expected.
   Can be rolled back by reverting to `db_timestamp` only.
2. **F3.2** (1-2 days): pre-warm endpoint. New resource, new test.
3. **F3.3** (2-3 days, optional): stale-while-revalidate. Most
   complex; requires celery task machinery.
4. **N3 regression test updated** to cover per-type invalidation
   (edit a person, verify event cache is still warm).

**Expected improvement**: with F3.1, the cache hit rate in a
session with mixed editing + browsing should jump from ~5% to ~70%.
With F3.2, manual cache control. With F3.3, the cold-path 14s
becomes a ~50ms stale response in the common case (great for
sporadic single-user scenarios).

### Phase 4 — Architectural (separate spec, weeks of work)

Out of scope for this spec. Tracked as a future item. The
single-tree `postgresql` deployment at 50K persons is acceptable
with Phases 1-3 done. Beyond that, the architectural fix becomes
necessary.

---

## 8. Test plan

### Existing tests

The repo's existing pytest suite (`tests/`) must continue to pass.
Particularly:

- `tests/test_resources.py` — exercises the list endpoints
- `tests/api/test_*.py` — per-endpoint coverage
- Any test that calls `/api/people/` with GQL/OQL/filter

### New tests (N3 regression test)

`tests/perf/test_cold_path.py`:

```python
@pytest.mark.perf
def test_cold_simple_page_under_5_seconds(fifty_k_person_db, jwt_headers):
    """Cold simple page should be <5s on a 50K-person tree."""
    db = fifty_k_person_db
    invalidate_request_cache()  # clear cache for the test
    t0 = time.time()
    resp = client.get("/api/people/?page=1&pagesize=20", headers=jwt_headers)
    elapsed = time.time() - t0
    assert resp.status_code == 200
    assert elapsed < 5.0, f"cold simple page took {elapsed:.2f}s"
```

```python
@pytest.mark.perf
def test_cold_gql_filter_under_10_seconds(fifty_k_person_db, jwt_headers):
    """Cold GQL filter should be <10s on a 50K-person tree."""
    db = fifty_k_person_db
    invalidate_request_cache()
    t0 = time.time()
    resp = client.get(
        "/api/people/?page=1&pagesize=20&gql=primary_name.first_name ~ 'John'",
        headers=jwt_headers,
    )
    elapsed = time.time() - t0
    assert resp.status_code == 200
    assert elapsed < 10.0, f"cold GQL took {elapsed:.2f}s"
```

`tests/perf/conftest.py` builds a 50K-person SQLite DB for the
benchmark. This is the regression guard for future changes.

### Manual verification

After each phase, against the user's live deployment:

1. **Phase 1**: `redis-cli -n 2 KEYS '*'` shows cache keys after
   a request. Warm latency <50ms.
2. **Phase 2.1**: `EXPLAIN ANALYZE` of the handles query shows
   no `ORDER BY` clause. `pg_stat_activity` during a slow
   request shows 1 query, not 2.
3. **Phase 2.2**: `pg_stat_activity` during a slow `extend=all`
   request shows ≤5 family-related queries, not 50+.
4. **Phase 2.3**: A test that would load 200,000 objects
   returns 413.
5. **Phase 3.1**: After a person edit, the event-list cache
   is still warm. After an event edit, the person-list cache
   is still warm.
6. **Phase 3.2**: `POST /api/admin/prewarm` with paths
   populates the cache; subsequent GETs to those paths
   return <50ms.

### Load testing

For the user's deployment (1 user, sporadic), formal load tests are
not needed. But the regression test (`test_cold_path.py`) should
also be runnable in a CI environment with a 50K-person DB seed
to catch regressions before they ship.

---

## 9. Rollout plan

The user's deployment is single-server, single-user. Rollout is
trivial:

1. **Phase 1**: deploy env-var changes; restart container. Zero
   downtime if done via a graceful gunicorn reload.
2. **Phase 2**: deploy code change; restart container. The change
   is to a hot path (`get()`), so there's a brief window where
   in-flight requests use the old code. For a single-user system
   this is fine.
3. **Phase 3**: deploy code change; restart. Same as Phase 2.
4. **No database migration** is required for any phase. The
   `changes` table already exists (Gramps maintains it).

For a multi-server deployment, each phase would need a staged
rollout (canary → 25% → 50% → 100%) and a kill-switch. Not
applicable here.

---

## 10. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| F2.1 changes default sort order, breaks UI expectations | low | low | Document the change; UI re-sorts client-side anyway; can be reverted in <5 min |
| F2.2 batch fetch returns stale data due to per-request cache | low | medium | Use `g` (flask request context) for the cache, automatically invalidated per request |
| F2.3 413 response breaks a script that didn't expect it | low | low | 413 is a new status code; document it; only fires for >100K result sets, which is exceptional |
| F3.1 `changes` table is incomplete or missing for some table types | medium | medium | Fall back to `db_timestamp` if `changes` query returns no rows; log a warning |
| F3.2 pre-warm endpoint abused for DoS | low | medium | Requires `PERM_VIEW_SETTINGS` (Admin only); rate-limit; cap the number of paths per call |
| F3.3 stale-while-revalidate hides important updates | medium | low | Use the standard `Cache-Control: max-age=...` semantics; the frontend can opt out; the `X-Cache: stale` header is visible to the user |
| Memory regression in worker | low | medium | N4 requirement; tested in N3 regression test |
| Cache stampede (8 workers all hit cold cache simultaneously) | medium | medium | F3.3 (stale-while-revalidate) addresses this; without it, the storm is bounded by URL diversity, not concurrency |

---

## 11. Open questions

1. **F3.1**: is the Gramps `change` table populated for **all** write
   operations, or just the ones Gramps considers "user-initiated"?
   If the latter, scripted imports might not invalidate the cache.
   Needs verification.
2. **F2.2**: what's the canonical Gramps API for batch-fetching
   families? `get_family_from_handle` per call is the only public
   method I see. May need a raw SQL query as a workaround, which
   would be a small upstream-PR-able contribution.
3. **F2.3**: should the cap be 100,000 (current proposal) or
   per-object-type (e.g., 50,000 persons but 1,000,000 events)?
   The current cap is conservative; events tend to be smaller than
   persons in real trees, so a single cap is probably fine.
4. **Phase 4**: when is the right time to start the GQL→SQL
   project? If the user's tree grows past 100K persons, the
   Phase 1-3 fixes stop being sufficient. Need to monitor
   `usage_people` and plan.

---

## 12. Acceptance criteria for "spec done"

The spec is "done" when all of these are true:

- [x] Phase 1 implemented in the user's prod deployment (already done)
- [ ] Phase 2.1 deployed; cold simple page <12s on user's prod
- [ ] Phase 2.2 deployed; cold GQL <100s on user's prod
- [ ] Phase 2.3 deployed; >100K result sets return 413
- [ ] Phase 3.1 deployed; per-type cache invalidation working
- [ ] Phase 3.2 deployed; pre-warm endpoint functional
- [ ] Phase 3.3 deployed; stale-while-revalidate working (optional)
- [ ] N3 regression test added and passing in CI
- [ ] All existing tests still pass
- [ ] User confirms the latency targets are met on their deployment

---

## 13. References

- [`MCP_PERFORMANCE_INVESTIGATION.md`](../MCP_PERFORMANCE_INVESTIGATION.md) —
  the source investigation; this spec is the implementation plan that
  flows from it
- [`BOOTSTRAP.md`](../BOOTSTRAP.md) — bootstrap runbook (out of scope
  for this spec; included for the deployment context)
- `gramps_webapi/api/resources/base.py:565-650` — the `get()` method
  this spec targets
- `gramps_webapi/api/resources/people.py:60-78` — N+1 fix location
- `gramps_webapi/api/cache.py:78-89` — cache key construction
- `gramps_webapi/config.py:55-72` — cache config defaults
- `CLAUDE.md` — project conventions for adding/modifying resources
