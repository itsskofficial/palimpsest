"""One command for the whole local stack.

    python scripts/dev.py up        # Supabase + migrations + bucket
    python scripts/dev.py serve     # ...and run the review app against it
    python scripts/dev.py status
    python scripts/dev.py env       # print the exports, for your own shell
    python scripts/dev.py reset     # wipe palimpsest's data, keep the stack
    python scripts/dev.py down

The point of running the **real** Supabase locally rather than SQLite is that the only
difference between here and production becomes credentials. Same Postgres, same
pgBouncer semantics, same Storage API, same RLS, same migrations. "It worked locally"
then means something.

Nothing here needs a key. The local stack's service-role key is a published constant
that only signs against a local secret. The keys you do eventually need — Notion,
Anthropic — are for things with no local equivalent, and much of the product runs
without them.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from palimpsest._console import install as _install_console  # noqa: E402

BUCKET = "palimpsest-archive"


def supabase_cmd() -> list[str]:
    """Prefer a real `supabase` on PATH; fall back to npx, which needs no install."""
    if shutil.which("supabase"):
        return ["supabase"]
    if shutil.which("npx"):
        return ["npx", "--yes", "supabase@2.113.0"]
    raise SystemExit(
        "the Supabase CLI is not available.\n"
        "  npm install -g supabase       (or) scoop install supabase\n"
        "  https://supabase.com/docs/guides/local-development"
    )


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=ROOT, **kw)


def heading(text: str) -> None:
    print(f"\n{'=' * 74}\n  {text}\n{'=' * 74}")


def apply_env() -> dict:
    """Resolve Supabase and put it in this process's environment."""
    from palimpsest.store import supabase as sb

    config = sb.detect(require=True)
    assert config is not None
    env = config.env()
    env["PALIMPSEST_ARTIFACT_URL"] = f"supabase://{BUCKET}/palimpsest"
    os.environ.update(env)
    return {"config": config, "env": env}


# ---------------------------------------------------------------------------


def cmd_up(args) -> int:
    heading("1. Local Supabase")
    from palimpsest.store.supabase import _local_is_running

    if _local_is_running():
        print("  already running")
    else:
        # The excluded services are the ones palimpsest does not use. Skipping them
        # saves a few hundred MB of images and a chunk of start-up time.
        result = run([*supabase_cmd(), "start", "-x",
                      "realtime,imgproxy,edge-runtime,logflare,vector,supavisor,mailpit"])
        if result.returncode != 0:
            return result.returncode

    info = apply_env()
    config = info["config"]
    print(f"  api      {config.api_url}")
    print(f"  db       {config.db_url}")
    print(f"  studio   {config.studio_url or 'http://127.0.0.1:54523'}")

    heading("2. Schema, RLS and the archive bucket")
    from palimpsest.cli import main as cli_main

    cli_main(["supabase", "init", "--bucket", BUCKET])

    heading("Ready")
    print("  python scripts/dev.py serve      # the review app on :8100")
    print("  python scripts/dev.py env        # exports for your own shell")
    print()
    print("  Then, with NOTION_TOKEN set:")
    print("    palimpsest sync                # pull your workspace into the mirror")
    print("    palimpsest sweep duplicates    # needs no model key")
    print()
    return 0


def cmd_serve(args) -> int:
    apply_env()
    from palimpsest import serve

    serve.run(port=args.port)
    return 0


def cmd_status(args) -> int:
    from palimpsest.store.supabase import _local_is_running

    if not _local_is_running():
        print("local Supabase is not running.  python scripts/dev.py up")
        return 1
    apply_env()
    from palimpsest.cli import main as cli_main

    return cli_main(["supabase", "status"])


def cmd_env(args) -> int:
    info = apply_env()
    prefix = "" if args.shell == "dotenv" else "export "
    for k, v in info["env"].items():
        print(f"{prefix}{k}={v}")
    return 0


def cmd_reset(args) -> int:
    """Drop palimpsest's rows, keep the stack and the schema."""
    apply_env()
    from palimpsest.store import open_store

    store = open_store(os.environ["PALIMPSEST_DATABASE_URL"])
    store.truncate_all()
    print(f"  truncated. {store.stats().summary()}")
    store.close()
    return 0


def cmd_down(args) -> int:
    return run([*supabase_cmd(), "stop"] + (["--no-backup"] if args.wipe else [])).returncode


def cmd_test(args) -> int:
    """Run the Postgres-marked tests against the running local stack."""
    apply_env()
    os.environ["PALIMPSEST_TEST_POSTGRES_URL"] = os.environ["PALIMPSEST_DATABASE_URL"]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "postgres or supabase_stack", "-v"],
        cwd=ROOT, env=env,
    ).returncode


def main() -> int:
    _install_console()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("up", help="start Supabase, migrate, create the archive bucket")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("serve", help="run the review app against local Supabase")
    sp.add_argument("--port", type=int, default=8100)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("status", help="what is running and what is in it")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("env", help="print the environment to export")
    sp.add_argument("--shell", default="bash", choices=["bash", "dotenv"])
    sp.set_defaults(func=cmd_env)

    sp = sub.add_parser("reset", help="truncate palimpsest's tables")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("down", help="stop the local Supabase stack")
    sp.add_argument("--wipe", action="store_true", help="also delete the data volume")
    sp.set_defaults(func=cmd_down)

    sp = sub.add_parser("test", help="run the Postgres-backed tests against the stack")
    sp.set_defaults(func=cmd_test)

    args = ap.parse_args()
    t0 = time.time()
    code = int(args.func(args) or 0)
    if args.command in ("up", "reset"):
        print(f"  ({time.time() - t0:.0f}s)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
