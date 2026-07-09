"""Unified search query language.

A small boolean query language shared by the board, the Ctrl-K palette and the
runs view. A query string is parsed once into a resource-agnostic AST, then
compiled to SQLAlchemy predicates against either tickets or runs.

Grammar (whitespace-separated terms are AND-ed)::

    expr     := or_expr
    or_expr  := and_expr ( "OR" and_expr )*
    and_expr := unary +                       # implicit AND between adjacents
    unary    := "NOT" unary | "-" atom | atom
    atom     := "(" expr ")" | field_cmp | text

``field_cmp`` is ``name OP value`` where OP is one of ``= : != > < >= <=`` and
``name`` is a recognised field. ``comma`` inside a value means OR-within-field
(``status=review,running``). A double-quoted run is a literal phrase. Bare words
fall through to full-text search over the ticket title and prompt.

The parser is resource-neutral: it recognises a field token only if the name is
in :data:`FIELD_ALIASES`. The compiler decides whether a given field applies to
the resource being searched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _dc_field
from datetime import date, datetime, time, timezone
from typing import Optional

from sqlalchemy import and_, column, false, func, not_, or_, select, table, text
from sqlalchemy.orm import Session, aliased

from nightdesk.db.models import Label, Profile, Project, Run, Ticket, ticket_labels


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MatchAll:
    """Empty query: imposes no constraint."""


@dataclass(frozen=True)
class Text:
    value: str
    phrase: bool = False


@dataclass(frozen=True)
class Cmp:
    field: str
    op: str
    value: str


@dataclass(frozen=True)
class Not:
    child: object


@dataclass(frozen=True)
class And:
    children: tuple


@dataclass(frozen=True)
class Or:
    children: tuple


Node = object


# Every field name (and alias) the language recognises, across all resources.
# A ``name=value`` token is only treated as a comparison when ``name`` is here.
FIELD_ALIASES: frozenset[str] = frozenset({
    "project", "status", "latest_status", "latest", "outcome", "profile",
    "backend", "model", "cost", "priority", "created", "started", "finished",
    "intent", "failure_kind", "label",
})

# Fields offered as facet dropdowns per resource (drives the UI + /search/suggest).
TICKET_FACETS: tuple[str, ...] = (
    "project", "status", "latest_status", "profile", "backend", "model", "label",
)
RUN_FACETS: tuple[str, ...] = (
    "outcome", "model", "project", "profile", "backend", "intent",
)


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #
_OP_RE = re.compile(r"^([A-Za-z_]+)(!=|>=|<=|=|:|>|<)(.*)$", re.S)


def _scan(s: str) -> list[tuple]:
    """Split a raw query into TERM / LPAREN / RPAREN tokens.

    A double quote protects spaces and parens so ``title="a b"`` and
    ``"a b"`` keep their internal whitespace. Parens that are not inside quotes
    are always their own token, even when flush against a term.
    """
    tokens: list[tuple] = []
    buf: list[str] = []
    quoted = False       # the term contains a quoted segment
    lead_quote = False   # the term began with a quote (a pure phrase)
    i, n = 0, len(s)

    def flush() -> None:
        nonlocal buf, quoted, lead_quote
        if buf or quoted:
            tokens.append(("TERM", "".join(buf), quoted, lead_quote))
        buf = []
        quoted = False
        lead_quote = False

    while i < n:
        c = s[i]
        if c == '"':
            if not buf:
                lead_quote = True
            quoted = True
            i += 1
            while i < n and s[i] != '"':
                buf.append(s[i])
                i += 1
            i += 1  # consume the closing quote (or step past EOF)
            continue
        if c.isspace():
            flush()
            i += 1
            continue
        if c == "(":
            flush()
            tokens.append(("LPAREN",))
            i += 1
            continue
        if c == ")":
            flush()
            tokens.append(("RPAREN",))
            i += 1
            continue
        buf.append(c)
        i += 1
    flush()
    return tokens


def _classify(tok: tuple) -> tuple:
    """Turn a TERM token into a parser token: OR / AND / NOT / NODE / SKIP."""
    _, raw, quoted, lead_quote = tok

    # A term that begins with a quote is a literal phrase: never a keyword or a
    # comparison, so operators inside it stay literal.
    if quoted and lead_quote:
        return ("SKIP",) if raw == "" else ("NODE", Text(raw, phrase=True))

    if not quoted:
        low = raw.lower()
        if low == "or":
            return ("OR",)
        if low == "and":
            return ("AND",)
        if low == "not":
            return ("NOT",)

    neg = False
    if not quoted and raw.startswith("-") and len(raw) > 1:
        neg = True
        raw = raw[1:]

    # ``project="my proj"`` reaches here with quoted=True, lead_quote=False: the
    # field/op is unquoted and only the value carried a quote, so parse it.
    m = _OP_RE.match(raw)
    if m and m.group(1).lower() in FIELD_ALIASES and m.group(3) != "":
        node: Node = Cmp(m.group(1).lower(), m.group(2), m.group(3))
    else:
        node = Text(raw, phrase=bool(quoted))
    return ("NODE", Not(node) if neg else node)


# --------------------------------------------------------------------------- #
# Parser (recursive descent)
# --------------------------------------------------------------------------- #
class _P:
    def __init__(self, toks: list[tuple]):
        self.toks = toks
        self.i = 0

    def peek(self) -> Optional[tuple]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def advance(self) -> tuple:
        t = self.toks[self.i]
        self.i += 1
        return t


def _parse_or(p: _P) -> Optional[Node]:
    left = _parse_and(p)
    while p.peek() == ("OR",):
        p.advance()
        right = _parse_and(p)
        parts: list[Node] = []
        for nd in (left, right):
            if nd is None:
                continue
            if isinstance(nd, Or):
                parts.extend(nd.children)
            else:
                parts.append(nd)
        left = Or(tuple(parts)) if len(parts) != 1 else parts[0]
    return left


def _parse_and(p: _P) -> Optional[Node]:
    children: list[Node] = []
    while True:
        t = p.peek()
        if t is None or t == ("RPAREN",) or t == ("OR",):
            break
        nd = _parse_unary(p)
        if nd is not None:
            children.append(nd)
    if not children:
        return None
    if len(children) == 1:
        return children[0]
    flat: list[Node] = []
    for nd in children:
        if isinstance(nd, And):
            flat.extend(nd.children)
        else:
            flat.append(nd)
    return And(tuple(flat))


def _parse_unary(p: _P) -> Optional[Node]:
    t = p.peek()
    if t is None:
        return None
    if t == ("NOT",):
        p.advance()
        child = _parse_unary(p)
        return Not(child) if child is not None else None
    if t == ("LPAREN",):
        p.advance()
        inner = _parse_or(p)
        if p.peek() == ("RPAREN",):
            p.advance()
        return inner
    if t is not None and t[0] == "NODE":
        p.advance()
        return t[1]
    # Stray RPAREN or anything unexpected: consume to guarantee progress.
    p.advance()
    return None


def parse_query(s: str) -> Node:
    """Parse a query string into an AST. Empty input yields :class:`MatchAll`."""
    if not s or not s.strip():
        return MatchAll()
    toks: list[tuple] = []
    for t in _scan(s):
        if t[0] != "TERM":
            toks.append(t)
            continue
        c = _classify(t)
        if c[0] in ("SKIP", "AND"):
            continue
        toks.append(c)
    if not toks:
        return MatchAll()
    node = _parse_or(_P(toks))
    return node if node is not None else MatchAll()


# --------------------------------------------------------------------------- #
# Compiler
# --------------------------------------------------------------------------- #
@dataclass
class _Ctx:
    session: Session
    resource: str
    latest_run: object = None
    n: int = 0
    # Cached resolver tables.
    _profiles: object = _dc_field(default=None)


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_iso_date(value: str) -> Optional[date]:
    v = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    # Accept a full ISO datetime, keep the date part.
    try:
        return datetime.fromisoformat(v).date()
    except ValueError:
        return None


def _date_pred(col, op: str, value: str):
    d = _parse_iso_date(value)
    if d is None:
        return false()
    iso = d.isoformat()
    # func.date() normalises any stored timestamp (naive or tz-suffixed) to
    # 'YYYY-MM-DD', so a lexical compare against an ISO date is chronological.
    dc = func.date(col)
    return {
        ">": dc > iso, "<": dc < iso, ">=": dc >= iso, "<=": dc <= iso,
        "=": dc == iso, ":": dc == iso, "!=": dc != iso,
    }.get(op, false())


def _number_pred(col, op: str, value: str):
    try:
        num = float(value)
    except ValueError:
        return false()
    return {
        ">": col > num, "<": col < num, ">=": col >= num, "<=": col <= num,
        "=": col == num, ":": col == num, "!=": col != num,
    }.get(op, false())


def _priority_pred(col, op: str, value: str):
    """Priority comparison that accepts both named values (``urgent``) and
    numeric strings (``3``). Named values are only resolved for equality
    operators (``= : !=``); range operators (``> >= < <=``) always use
    numeric comparison so ``priority>=3`` keeps working."""
    from nightdesk.domain.priority import is_priority_name, resolve_priority
    if op in ("=", ":", "!=") and is_priority_name(value):
        resolved = resolve_priority(value)
        if resolved is None:
            return false()
        return {
            "=": col == resolved, ":": col == resolved,
            "!=": col != resolved,
        }.get(op, false())
    # Numeric or range comparison.
    return _number_pred(col, op, value)


def _string_pred(col, op: str, value: str):
    if op == ":":
        return col.ilike(f"%{value}%")
    if op == "!=":
        return or_(col.is_(None), col != value)
    return col == value


def _enum_pred(col, op: str, value: str):
    values = _split_csv(value)
    if not values:
        return None
    base = col.in_(values) if len(values) > 1 else col == values[0]
    return not_(base) if op == "!=" else base


def _profiles(ctx: _Ctx):
    if ctx._profiles is None:
        ctx._profiles = list(ctx.session.scalars(select(Profile)))
    return ctx._profiles


def _project_pred(ctx: _Ctx, op: str, value: str):
    ids: list[str] = []
    want_null = False
    for v in _split_csv(value):
        if v in ("none", "null"):
            want_null = True
            continue
        proj = ctx.session.scalars(
            select(Project).where(Project.slug == v)
        ).first()
        ids.append(proj.id if proj else "__missing__")
    preds = []
    if ids:
        preds.append(Ticket.project_id.in_(ids))
    if want_null:
        preds.append(Ticket.project_id.is_(None))
    if not preds:
        return None
    base = or_(*preds) if len(preds) > 1 else preds[0]
    return not_(base) if op == "!=" else base


def _profile_pred(ctx: _Ctx, op: str, value: str):
    wanted = {v.lower() for v in _split_csv(value)}
    ids = [p.id for p in _profiles(ctx) if p.name.lower() in wanted] or ["__missing__"]
    base = Ticket.profile_id.in_(ids)
    return not_(base) if op == "!=" else base


def _backend_pred(ctx: _Ctx, op: str, value: str):
    wanted = {v.lower() for v in _split_csv(value)}
    ids = [p.id for p in _profiles(ctx) if (p.backend or "").lower() in wanted] \
        or ["__missing__"]
    base = Ticket.profile_id.in_(ids)
    return not_(base) if op == "!=" else base


def _latest_status_pred(ctx: _Ctx, op: str, value: str):
    lr = ctx.latest_run
    preds = []
    for v in _split_csv(value):
        if v in ("none", "null"):
            preds.append(lr.id.is_(None))
        elif v == "running":
            preds.append(and_(lr.id.isnot(None), lr.exit_status.is_(None)))
        else:
            preds.append(lr.exit_status == v)
    if not preds:
        return None
    base = or_(*preds) if len(preds) > 1 else preds[0]
    return not_(base) if op == "!=" else base


def _run_outcome_pred(op: str, value: str):
    preds = []
    for v in _split_csv(value):
        if v in ("none", "null"):
            preds.append(Run.exit_status.is_(None))
        elif v == "running":
            preds.append(and_(Run.finished_at.is_(None), Run.exit_status.is_(None)))
        else:
            preds.append(Run.exit_status == v)
    if not preds:
        return None
    base = or_(*preds) if len(preds) > 1 else preds[0]
    return not_(base) if op == "!=" else base


def _fts_match_string(value: str, phrase: bool) -> str:
    v = value.strip()
    if not v:
        return ""
    if phrase:
        return '"' + v.replace('"', "") + '"'
    toks = [f'"{t}"*' for t in v.split() if t.replace('"', "")]
    return " ".join(toks)


def _fts_select(ctx: _Ctx, node: Text):
    """Ticket ids matching a free-text term via the FTS5 index.

    The index is kept complete and in-sync by ``ensure_fts_index`` at startup,
    so this stays a fast, ranked, word-prefix search.
    """
    match = _fts_match_string(node.value, node.phrase)
    if not match:
        return None
    ctx.n += 1
    pname = f"_fts{ctx.n}"
    fts = table("tickets_fts", column("id"))
    clause = text(f"tickets_fts MATCH :{pname}").bindparams(**{pname: match})
    return select(fts.c.id).where(clause)


def _label_pred(ctx: _Ctx, op: str, value: str):
    """Ticket has a label matching the given name(s)."""
    wanted = {v.strip().lower() for v in _split_csv(value)}
    label_ids = [
        l.id for l in ctx.session.scalars(select(Label)).all()
        if l.name.lower() in wanted
    ] or ["__missing__"]
    sub = select(ticket_labels.c.ticket_id).where(
        ticket_labels.c.label_id.in_(label_ids)
    )
    if op == "!=":
        return ~Ticket.id.in_(sub)
    return Ticket.id.in_(sub)


def _ticket_predicate(ctx: _Ctx, field: str, op: str, value: str):
    if field == "project":
        return _project_pred(ctx, op, value)
    if field == "status":
        return _enum_pred(Ticket.status, op, value)
    if field in ("latest_status", "latest", "outcome"):
        return _latest_status_pred(ctx, op, value)
    if field == "profile":
        return _profile_pred(ctx, op, value)
    if field == "backend":
        return _backend_pred(ctx, op, value)
    if field == "model":
        return _string_pred(ctx.latest_run.model_used, op, value)
    if field == "cost":
        return _number_pred(ctx.latest_run.cost_usd, op, value)
    if field == "priority":
        return _priority_pred(Ticket.priority, op, value)
    if field == "created":
        return _date_pred(Ticket.created_at, op, value)
    if field == "label":
        return _label_pred(ctx, op, value)
    return false()


def _run_predicate(ctx: _Ctx, field: str, op: str, value: str):
    if field in ("status", "outcome", "latest_status", "latest"):
        return _run_outcome_pred(op, value)
    if field == "model":
        return _string_pred(Run.model_used, op, value)
    if field == "cost":
        return _number_pred(Run.cost_usd, op, value)
    if field == "project":
        return _project_pred(ctx, op, value)
    if field == "profile":
        return _profile_pred(ctx, op, value)
    if field == "backend":
        return _backend_pred(ctx, op, value)
    if field in ("started", "created"):
        return _date_pred(Run.started_at, op, value)
    if field == "finished":
        return _date_pred(Run.finished_at, op, value)
    if field == "intent":
        return _enum_pred(Run.intent, op, value)
    if field == "failure_kind":
        return _string_pred(Run.failure_kind, op, value)
    if field == "priority":
        return _priority_pred(Ticket.priority, op, value)
    return false()


def _compile(node: Node, ctx: _Ctx):
    if isinstance(node, MatchAll):
        return None
    if isinstance(node, Cmp):
        if ctx.resource == "run":
            return _run_predicate(ctx, node.field, node.op, node.value)
        return _ticket_predicate(ctx, node.field, node.op, node.value)
    if isinstance(node, Text):
        sub = _fts_select(ctx, node)
        if sub is None:
            return None
        if ctx.resource == "run":
            return Run.ticket_id.in_(sub)
        return Ticket.id.in_(sub)
    if isinstance(node, Not):
        inner = _compile(node.child, ctx)
        return not_(inner) if inner is not None else None
    if isinstance(node, And):
        parts = [c for c in (_compile(ch, ctx) for ch in node.children) if c is not None]
        return and_(*parts) if parts else None
    if isinstance(node, Or):
        parts = [c for c in (_compile(ch, ctx) for ch in node.children) if c is not None]
        return or_(*parts) if parts else None
    return None


# --------------------------------------------------------------------------- #
# Public search helpers
# --------------------------------------------------------------------------- #
def search_tickets(
    session: Session,
    ast: Node,
    *,
    status: Optional[str] = None,
    limit: int = 500,
) -> list[Ticket]:
    """Tickets matching the parsed query, optionally narrowed to one status.

    The board passes ``status`` per column so the kanban layout survives: any
    ``status=`` term in the query simply empties the columns it excludes.
    """
    lr = aliased(Run)
    ctx = _Ctx(session=session, resource="ticket", latest_run=lr)
    where = _compile(ast, ctx)
    stmt = select(Ticket).outerjoin(lr, lr.id == Ticket.current_run_id)
    if where is not None:
        stmt = stmt.where(where)
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    stmt = stmt.order_by(
        Ticket.position.asc(), Ticket.priority.desc(), Ticket.created_at.asc()
    ).limit(limit)
    try:
        return list(session.scalars(stmt).unique())
    except Exception:
        # A malformed FTS MATCH string must not 500 the board poll; mirror the
        # FTS backend's swallow-and-empty behaviour.
        return []


def search_runs(session: Session, ast: Node, *, limit: int = 200) -> list[Run]:
    """Runs matching the parsed query, newest first."""
    ctx = _Ctx(session=session, resource="run")
    where = _compile(ast, ctx)
    stmt = (
        select(Run)
        .join(Ticket, Run.ticket_id == Ticket.id)
    )
    if where is not None:
        stmt = stmt.where(where)
    stmt = stmt.order_by(Run.started_at.desc()).limit(limit)
    try:
        return list(session.scalars(stmt).unique())
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Value suggestions (facet dropdowns + autocomplete)
# --------------------------------------------------------------------------- #
_TICKET_STATUSES = ("draft", "queued", "running", "review", "archived")
_OUTCOMES = ("success", "failed", "cancelled", "worker_crash", "running", "none")


def suggest_values(
    session: Session,
    *,
    resource: str = "ticket",
    field: str,
    prefix: str = "",
    limit: int = 25,
) -> list[dict]:
    """Existing values for a field, for the facet dropdowns and autocomplete.

    Returns ``[{"value": ..., "label": ...}]``. ``value`` is what goes into the
    query string; ``label`` is what the user sees (e.g. project name vs slug).
    """
    field = (field or "").lower()
    pref = (prefix or "").strip().lower()
    out: list[dict] = []

    def add(value: str, label: Optional[str] = None) -> None:
        out.append({"value": value, "label": label if label is not None else value})

    if field == "project":
        add("none", "No project")
        for p in session.scalars(select(Project).order_by(Project.position.asc())):
            add(p.slug, p.name)
    elif field == "status":
        # In the runs view, ``status`` means the run's exit status.
        for s in (_OUTCOMES if resource == "run" else _TICKET_STATUSES):
            add(s)
    elif field in ("latest_status", "latest", "outcome"):
        for s in _OUTCOMES:
            add(s)
    elif field == "profile":
        for p in session.scalars(select(Profile).order_by(Profile.name.asc())):
            add(p.name)
    elif field == "backend":
        seen: set[str] = set()
        for (b,) in session.execute(select(Profile.backend).distinct()):
            if b and b not in seen:
                seen.add(b)
                add(b)
    elif field == "model":
        seen = set()
        for (m,) in session.execute(select(Run.model_used).distinct()):
            if m and m not in seen:
                seen.add(m)
                add(m)
    elif field == "intent":
        seen = set()
        for (it,) in session.execute(select(Run.intent).distinct()):
            if it and it not in seen:
                seen.add(it)
                add(it)
    elif field == "priority":
        from nightdesk.domain.priority import PRIORITY_SCALE
        for s in PRIORITY_SCALE:
            add(s["name"], s["label"])
    elif field == "label":
        for l in session.scalars(select(Label).order_by(Label.name.asc())):
            add(l.name)

    if pref:
        out = [o for o in out
               if pref in o["value"].lower() or pref in o["label"].lower()]
    return out[:limit]
