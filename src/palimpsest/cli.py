"""The `palimpsest` command.

argparse, no framework dependency: `pip install palimpsest` should give a working
command with nothing else pulled in.

The command surface follows the safety model rather than the module layout. `ingest`
proposes and never writes; `apply` writes and demands a reviewer name; `undo` reverses
exactly. You have to type a different command to change your notes than to look at
them, which is the intended amount of friction.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from palimpsest._console import install as _install_console
from palimpsest._version import __version__

__all__ = ["build_parser", "main"]


def _settings(**overrides):
    from palimpsest.config import Settings

    try:
        return Settings.load(**overrides)
    except ValueError as e:
        raise SystemExit(f"configuration error:\n{e}") from e


def _store(args):
    from palimpsest.store import open_store

    settings = _settings(database_url=getattr(args, "db", None))
    return open_store(settings.database_url), settings


def _model(settings, required: bool = True):
    from palimpsest.llm import Model, ModelError

    try:
        return Model(settings.model, api_key=settings.anthropic_api_key,
                     max_tokens=settings.max_tokens)
    except (ModelError, ImportError) as e:
        if required:
            raise SystemExit(str(e)) from e
        return None


def _notion(settings):
    from palimpsest.notion.client import NotionClient

    try:
        return NotionClient(settings.notion_token or "", version=settings.notion_version)
    except ValueError as e:
        raise SystemExit(str(e)) from e


def _emit(payload, out: str | None) -> None:
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                             encoding="utf-8")
        print(f"\nwrote {out}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_sync(args) -> int:
    """Pull Notion into the local mirror."""
    from palimpsest.notion import mirror

    store, settings = _store(args)
    client = _notion(settings)

    try:
        who = client.whoami()
        print(f"integration: {who.get('name') or who.get('id', '?')}")
    except Exception as e:
        raise SystemExit(
            f"could not authenticate with Notion: {e}\n"
            "Check NOTION_TOKEN, and remember an integration sees nothing until you "
            "share at least one page with it."
        ) from e

    def progress(i: int, total: int, title: str) -> None:
        sys.stdout.write(f"\r  [{i}/{total}] {title[:56]:<56}")
        sys.stdout.flush()

    result = mirror.sync(client, store, incremental=not args.full,
                         roots=settings.notion_root_pages, limit=args.limit,
                         on_progress=progress if not args.quiet else None)
    print("\r" + " " * 74 + "\r", end="")
    print(result.summary())
    for err in result.errors[:10]:
        print(f"  ! {err}")
    _emit(result.as_dict(), args.out)
    store.close()
    return 0


def cmd_ingest(args) -> int:
    """Run a source through the pipeline. Writes nothing to Notion."""
    from palimpsest.artifacts import open_artifacts
    from palimpsest.pipeline import ingest

    store, settings = _store(args)
    model = _model(settings)
    archive = open_artifacts(settings.artifact_url)

    try:
        result = ingest(args.spec, store, model, settings=settings, kind=args.kind,
                        archive=archive, reuse=not args.fresh,
                        max_windows=args.max_windows)
    except (RuntimeError, ValueError, ImportError) as e:
        raise SystemExit(str(e)) from e

    print(result.summary())
    if result.patch.operations:
        print(f"\n  patch: {result.patch.patch_id}")
        for op in result.patch.operations[:20]:
            rel = op.relation.value if op.relation else "-"
            print(f"    [{rel:<12}] {op.summary()[:96]}")
        if len(result.patch.operations) > 20:
            print(f"    … and {len(result.patch.operations) - 20} more")
        print(f"\n  review it:  palimpsest patch {result.patch.patch_id}")
        print(f"  apply it:   palimpsest apply {result.patch.patch_id} "
              f"--reviewer \"$(whoami)\"")
    if result.review:
        print(f"\n  {len(result.review)} item(s) need you:")
        for item in result.review[:10]:
            rel = item["judgement"]["relation"]
            print(f"    [{rel:<12}] {item['claim']['text'][:80]}")
            if item.get("existing_text"):
                print(f"      existing: {item['existing_text'][:78]}")
            print(f"      {item['judgement']['rationale'][:88]}")
    _emit(result.as_dict(), args.out)
    store.close()
    return 0


def cmd_patch(args) -> int:
    """Show a proposed patch."""
    store, _ = _store(args)
    patch = store.get_patch(args.patch_id)
    if patch is None:
        raise SystemExit(f"no patch {args.patch_id}")
    print(patch.summary())
    for op in patch.operations:
        rel = op.relation.value if op.relation else "-"
        flag = "applied" if op.applied else "      "
        print(f"  {flag} [{rel:<12}] {op.op_id[:10]} {op.summary()[:88]}")
    _emit(patch.as_dict(), args.out)
    store.close()
    return 0


def cmd_patches(args) -> int:
    """List patches."""
    store, _ = _store(args)
    rows = store.list_patches(status=args.status, limit=args.limit)
    if not rows:
        print("no patches")
    for r in rows:
        print(f"  {r['patch_id']}  {r['status']:<9} {r['n_ops']:>3} op(s)  "
              f"{r.get('reviewer') or '-'}")
    store.close()
    return 0


def cmd_apply(args) -> int:
    """Apply a patch to Notion. The only command that changes your notes."""
    from palimpsest.notion.apply import apply_patch
    from palimpsest.types import Relation

    store, settings = _store(args)
    patch = store.get_patch(args.patch_id)
    if patch is None:
        raise SystemExit(f"no patch {args.patch_id}")

    blocked = [op for op in patch.operations if op.relation is Relation.CONTRADICTS]
    if blocked:
        raise SystemExit(
            f"{len(blocked)} operation(s) derive from a contradiction. Those are never "
            "applied automatically - resolve them on the page yourself."
        )
    if not args.dry_run and not args.reviewer:
        raise SystemExit("--reviewer is required: an applied change records who approved it")

    client = _notion(settings)
    result = apply_patch(client, store, patch, dry_run=args.dry_run,
                         reviewer=args.reviewer)
    print(("dry run: " if args.dry_run else "") + result.summary())
    for err in result.errors:
        print(f"  ! {err}")
    if not args.dry_run and result.applied:
        print(f"\n  undo:  palimpsest undo {patch.patch_id}")
    store.close()
    return 1 if result.failed else 0


def cmd_undo(args) -> int:
    """Revert an applied patch."""
    from palimpsest.notion.apply import revert_patch

    store, settings = _store(args)
    patch = store.get_patch(args.patch_id)
    if patch is None:
        raise SystemExit(f"no patch {args.patch_id}")
    client = _notion(settings)
    result = revert_patch(client, store, patch, reviewer=args.reviewer)
    print(result.summary())
    for err in result.errors:
        print(f"  ! {err}")
    store.close()
    return 1 if result.failed else 0


def cmd_sweep(args) -> int:
    """Run a sweep over the notes you already have."""
    from palimpsest import sweep as sweeps

    store, settings = _store(args)
    if args.kind == "duplicates":
        result = sweeps.duplicates(store, threshold=args.threshold, top=args.top)
    elif args.kind == "stale":
        result = sweeps.stale(store, top=args.top)
    elif args.kind == "questions":
        result = sweeps.open_questions(store, top=args.top)
    elif args.kind == "contradictions":
        result = sweeps.contradictions(store, _model(settings), max_pairs=args.max_pairs)
    else:  # pragma: no cover - argparse constrains this
        raise SystemExit(f"unknown sweep {args.kind!r}")

    print(result.summary())
    for note in result.notes:
        print(f"  - {note}")
    for f in result.findings[: args.show]:
        if args.kind in ("duplicates", "contradictions"):
            head = f.get("kind", "duplicate")
            print(f"\n  [{head}] similarity {f['similarity']}"
                  + (f" - confidence {f['confidence']}" if "confidence" in f else ""))
            if f.get("explanation"):
                print(f"    {f['explanation']}")
            print(f"    A - {f['a']['page_title']}: {f['a']['text'][:110]}")
            print(f"    B - {f['b']['page_title']}: {f['b']['text'][:110]}")
        elif args.kind == "stale":
            print(f"  {f['staleness']:>5}×  {f['page_title'][:28]:<28} {f['text'][:60]}")
        else:
            print(f"  {f['page_title'][:28]:<28} {f['text'][:80]}")
    _emit(result.as_dict(), args.out)
    store.close()
    return 0


def cmd_serve(args) -> int:
    from palimpsest import serve

    serve.run(host=args.host, port=args.port, db=args.db)
    return 0


def cmd_status(args) -> int:
    """Show configuration, mirror size and anything that looks wrong."""
    store, settings = _store(args)
    print("palimpsest configuration")
    print(settings.summary())
    print("\nstore")
    print(f"  {store.stats().summary()}")
    print(f"  migrations  {store.applied_migrations()}")
    problems = settings.problems()
    print("\nchecks")
    for p in problems:
        print(f"  ! {p}")
    if not problems:
        print("  all good")
    store.close()
    return 1 if problems else 0


def cmd_db(args) -> int:
    from palimpsest.store import open_store
    from palimpsest.store.migrations import MIGRATIONS, postgres_sql, sqlite_sql

    if args.db_command == "sql":
        sql = sqlite_sql() if args.dialect == "sqlite" else postgres_sql()
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(sql, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            print(sql)
        return 0

    settings = _settings()
    url = args.url or settings.database_url

    if args.db_command == "check":
        from palimpsest.config import redact

        print(f"database  {redact(url, 'url')}")
        try:
            store = open_store(url)
        except Exception as e:
            print(f"  UNREACHABLE: {type(e).__name__}: {e}")
            return 1
        applied = store.applied_migrations()
        pending = [m.id for m in MIGRATIONS if m.id not in set(applied)]
        print(f"  ping      {store.ping() * 1000:.1f} ms")
        print(f"  applied   {applied or 'none'}")
        print(f"  pending   {pending or 'none'}")
        print(f"  contents  {store.stats().summary()}")
        store.close()
        return 0 if not pending else 2

    if args.db_command == "migrate":
        store = open_store(url)
        applied = store.migrate()
        print(f"applied {len(applied)} migration(s): {applied or 'nothing to do'}")
        store.close()
        return 0

    if args.db_command == "reset":
        if not args.yes:
            raise SystemExit("this drops the mirror and the whole patch ledger. "
                             "Re-run with --yes if you mean it.")
        store = open_store(url)
        store.truncate_all()
        print(f"truncated. {store.stats().summary()}")
        store.close()
        return 0

    raise SystemExit(f"unknown db command {args.db_command!r}")


def cmd_supabase(args) -> int:
    """Point palimpsest at Supabase - the local stack or a cloud project."""
    from palimpsest.store import supabase as sb

    if args.supabase_command == "url":
        if not (args.project_ref and args.password):
            raise SystemExit(
                "need --project-ref and --password.\n"
                "  Supabase dashboard -> Project Settings -> Database -> Connection string")
        print(sb.connection_url(args.project_ref, args.password,
                                purpose=args.purpose, region=args.region))
        return 0

    config = sb.detect(require=True)
    assert config is not None

    if args.supabase_command == "status":
        print(f"supabase [{'local' if config.is_local else 'cloud'}]")
        for k, v in config.as_dict().items():
            print(f"  {k:<18} {v}")
        try:
            from palimpsest.store import open_store

            store = open_store(config.db_url)
            print(f"  {'database':<18} reachable, {store.ping() * 1000:.1f} ms")
            print(f"  {'migrations':<18} {store.applied_migrations()}")
            print(f"  {'contents':<18} {store.stats().summary()}")
            store.close()
        except Exception as e:
            print(f"  {'database':<18} UNREACHABLE: {type(e).__name__}: {e}")
            return 1
        return 0

    if args.supabase_command == "env":
        prefix = "" if args.shell == "dotenv" else "export "
        for k, v in config.env().items():
            print(f"{prefix}{k}={v}")
        print(f"{prefix}PALIMPSEST_ARTIFACT_URL=supabase://{args.bucket}/palimpsest")
        return 0

    if args.supabase_command == "init":
        from palimpsest.artifacts import SupabaseArtifacts
        from palimpsest.store import open_store

        print(f"supabase [{'local' if config.is_local else 'cloud'}]  {config.api_url}")
        store = open_store(config.db_url)
        applied = store.migrate()
        print(f"  migrations   {applied or 'already up to date'}")
        store.close()

        if config.service_role_key:
            artifacts = SupabaseArtifacts(args.bucket, "palimpsest",
                                          url=config.api_url,
                                          service_key=config.service_role_key)
            info = artifacts.create_bucket(public=False)
            print(f"  bucket       {info['bucket']} "
                  f"({'created' if info['created'] else 'already exists'}, private)")
            probe = artifacts.put("_healthcheck.json", {"ok": True})
            print(f"  storage      write+read ok -> {probe.uri}")
        else:
            print("  bucket       skipped: no SUPABASE_SERVICE_ROLE_KEY")

        print("\n  export these to use it:")
        for k, v in config.env().items():
            print(f"    export {k}={v}")
        print(f"    export PALIMPSEST_ARTIFACT_URL=supabase://{args.bucket}/palimpsest")
        return 0

    raise SystemExit(f"unknown supabase command {args.supabase_command!r}")


def cmd_history(args) -> int:
    """Every applied change to a page, newest first."""
    store, _ = _store(args)
    page = store.get_page(args.page_id)
    if page is None:
        raise SystemExit(f"no page {args.page_id} in the mirror")
    print(f"{page.get('title')}  ({args.page_id})")
    rows = store.page_history(args.page_id, limit=args.limit)
    if not rows:
        print("  no recorded changes - palimpsest has not edited this page")
    for r in rows:
        import time as _t

        when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(r["applied_at"] or 0))
        state = "reverted" if r.get("reverted_at") else "applied "
        print(f"  {when}  {state}  {r['kind']:<16} {r.get('relation') or '-':<12} "
              f"{r['op_id'][:10]}")
    store.close()
    return 0


def cmd_provenance(args) -> int:
    """Which source produced the text in a block."""
    store, _ = _store(args)
    rows = store.provenance_for_block(args.block_id)
    if not rows:
        print("no provenance recorded for that block")
        return 0
    for r in rows:
        anchor = r.get("anchor") or {}
        print(f"  {r.get('source_title') or r['source_id']}")
        print(f"    relation {r.get('relation') or '-'}   "
              f"locator {anchor.get('locator') or '-'}")
        if anchor.get("url"):
            print(f"    {anchor['url']}")
    store.close()
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="palimpsest",
        description="A self-maintaining knowledge base on top of Notion.",
    )
    p.add_argument("--version", action="version", version=f"palimpsest {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, db: bool = True, out: bool = True):
        if db:
            sp.add_argument("--db", default=None, help="defaults to PALIMPSEST_DATABASE_URL")
        if out:
            sp.add_argument("--out", default=None, help="write the full result as JSON")

    sp = sub.add_parser("sync", help="pull Notion into the local mirror")
    common(sp)
    sp.add_argument("--full", action="store_true",
                    help="refetch everything, and detect pages that disappeared")
    sp.add_argument("--limit", type=int, default=None, help="stop after N pages")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("ingest", help="run a source through the pipeline (writes nothing)")
    sp.add_argument("spec", help="a URL, a file path, or text:'...'")
    common(sp)
    sp.add_argument("--kind", default=None,
                    choices=["web", "youtube", "pdf", "image", "tabular", "text"])
    sp.add_argument("--fresh", action="store_true", help="re-extract even if already ingested")
    sp.add_argument("--max-windows", type=int, default=None,
                    help="cap extraction windows (useful for a cheap first look)")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("patches", help="list patches")
    common(sp, out=False)
    sp.add_argument("--status", default=None,
                    choices=["proposed", "applied", "rejected", "reverted", "partial"])
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_patches)

    sp = sub.add_parser("patch", help="show one patch")
    sp.add_argument("patch_id")
    common(sp)
    sp.set_defaults(func=cmd_patch)

    sp = sub.add_parser("apply", help="apply a patch to Notion")
    sp.add_argument("patch_id")
    common(sp, out=False)
    sp.add_argument("--reviewer", default=None, help="who approved this (required)")
    sp.add_argument("--dry-run", action="store_true",
                    help="walk the patch and build every inverse, writing nothing")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("undo", help="revert an applied patch, exactly")
    sp.add_argument("patch_id")
    common(sp, out=False)
    sp.add_argument("--reviewer", default=None)
    sp.set_defaults(func=cmd_undo)

    sp = sub.add_parser("sweep", help="audit the notes you already have")
    sp.add_argument("kind", choices=["duplicates", "contradictions", "stale", "questions"])
    common(sp)
    sp.add_argument("--threshold", type=float, default=0.32,
                    help="duplicates: similarity bar (lower finds paraphrases)")
    sp.add_argument("--top", type=int, default=50)
    sp.add_argument("--show", type=int, default=12, help="how many to print")
    sp.add_argument("--max-pairs", type=int, default=120,
                    help="contradictions: cap the pairs sent to the model")
    sp.set_defaults(func=cmd_sweep)

    sp = sub.add_parser("serve", help="the review app on :8100")
    sp.add_argument("--host", default=None)
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--db", default=None)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("status", help="configuration, mirror size, and what looks wrong")
    common(sp, out=False)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("history", help="every applied change to a page")
    sp.add_argument("page_id")
    common(sp, out=False)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("provenance", help="which source produced a block's text")
    sp.add_argument("block_id")
    common(sp, out=False)
    sp.set_defaults(func=cmd_provenance)

    sp = sub.add_parser("db", help="migrate, check, reset or dump the schema")
    sp.add_argument("db_command", choices=["check", "migrate", "sql", "reset"])
    sp.add_argument("--url", default=None)
    sp.add_argument("--dialect", default="postgres", choices=["postgres", "sqlite"])
    sp.add_argument("--out", default=None)
    sp.add_argument("--yes", action="store_true", help="required by `reset`")
    sp.set_defaults(func=cmd_db)

    sp = sub.add_parser("supabase", help="local or cloud Supabase: status, init, env, url")
    sp.add_argument("supabase_command", choices=["status", "init", "env", "url"])
    sp.add_argument("--bucket", default="palimpsest-archive")
    sp.add_argument("--shell", default="bash", choices=["bash", "dotenv"])
    sp.add_argument("--project-ref", default=None)
    sp.add_argument("--password", default=None)
    sp.add_argument("--purpose", default="service",
                    choices=["service", "session", "direct"])
    sp.add_argument("--region", default="ap-south-1")
    sp.set_defaults(func=cmd_supabase)

    return p


def main(argv: list[str] | None = None) -> int:
    # Before anything prints: a Notion page title with an em dash must not kill the
    # command on a legacy Windows code page.
    _install_console()
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
