"""
speed_assistant.py - OPTIONAL speed helpers for PayLock.

Safe to delete: app.py imports this file inside try/except and works the same without it.
Contains ONLY generic helpers. No fee, split, price, Paystack, PIN, escrow or auth logic lives here,
and nothing here reads or logs secrets.

  request_cached(key, fn)   remember a value for the CURRENT request only (never shared between users)
  clamp_page(page, limit)   safe page / limit numbers (limit never above 20)
  paginate(query, page)     add LIMIT 20 + offset to a Supabase query builder
  fast_list(...)            list rows with ONLY the fields you ask for (never select "*")
  fetch_many_by_ids(...)    ONE query for many ids (replaces a query inside a loop = N+1)
  install(app)              gzip text / JSON responses (index.html shrinks about 5x); skips files and errors
"""

import gzip

PAGE_SIZE = 20          # the only page size: LIMIT 20
_ID_CHUNK = 100         # ids per query, keeps the request URL short
_MIN_GZIP_BYTES = 1024
_GZIP_TYPES = ("text/html", "text/css", "text/plain", "application/json", "application/javascript", "text/javascript")


def request_cached(key, fn):
    """Return fn() once per request. Stored on flask.g, so nothing leaks between requests or users."""
    try:
        from flask import g, has_request_context
    except Exception:
        return fn()
    if not has_request_context():
        return fn()
    store = g.__dict__.setdefault("_speed_cache", {})
    if key not in store:
        store[key] = fn()
    return store[key]


def clamp_page(page=1, limit=PAGE_SIZE):
    """-> (page >= 1, limit between 1 and 20). Bad input falls back to page 1 / 20 rows."""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = PAGE_SIZE
    return page, max(1, min(limit, PAGE_SIZE))


def paginate(query, page=1, limit=PAGE_SIZE):
    """Apply LIMIT (max 20) and the page offset to a Supabase query builder."""
    page, limit = clamp_page(page, limit)
    start = (page - 1) * limit
    return query.range(start, start + limit - 1)


def fast_list(supabase, table, fields, filters=None, order=None, desc=True, page=1, limit=PAGE_SIZE):
    """
    Rows from `table` with ONLY `fields` (a list or comma string). filters = {"column": value}.
    Returns a list of dicts. Raises whatever the database raises, so the caller keeps its own error handling.
    """
    cols = fields if isinstance(fields, str) else ",".join(fields)
    if not cols or cols.strip() == "*":
        raise ValueError("fast_list needs an explicit field list")
    q = supabase.table(table).select(cols)
    for column, value in (filters or {}).items():
        q = q.eq(column, value)
    if order:
        q = q.order(order, desc=bool(desc))
    return paginate(q, page, limit).execute().data or []


def fetch_many_by_ids(supabase, table, id_column, ids, fields):
    """
    One query per 100 ids instead of one per id. Returns {id: row}. Rows must include `id_column` in `fields`.
    Use it to replace "for each order, fetch its product" loops.
    """
    cols = fields if isinstance(fields, str) else ",".join(fields)
    wanted = [i for i in dict.fromkeys(ids or []) if i is not None]
    out = {}
    for n in range(0, len(wanted), _ID_CHUNK):
        rows = supabase.table(table).select(cols).in_(id_column, wanted[n:n + _ID_CHUNK]).execute().data or []
        for row in rows:
            out[row.get(id_column)] = row
    return out


def install(app):
    """Compress text / JSON responses with gzip when the browser accepts it. Never raises, never changes a body's meaning."""
    if getattr(app, "_speed_gzip", False):
        return

    @app.after_request
    def _gzip(resp):
        try:
            from flask import request
            if (resp.status_code != 200 or resp.is_streamed and not resp.direct_passthrough
                    or "Content-Encoding" in resp.headers or "gzip" not in request.headers.get("Accept-Encoding", "").lower()):
                return resp
            if resp.mimetype not in _GZIP_TYPES or "attachment" in resp.headers.get("Content-Disposition", ""):
                return resp
            resp.direct_passthrough = False          # index.html is a file response; read it as normal bytes (text types only)
            data = resp.get_data()
            if len(data) < _MIN_GZIP_BYTES:
                return resp
            resp.set_data(gzip.compress(data, compresslevel=6))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Content-Length"] = str(len(resp.get_data()))
            resp.headers.add("Vary", "Accept-Encoding")
        except Exception:
            pass          # on any problem the original response goes out unchanged
        return resp

    app._speed_gzip = True
