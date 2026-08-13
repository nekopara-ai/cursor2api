"""Command line entry point.

    python -m cursor2api serve            start the Anthropic/OpenAI-compatible server
    python -m cursor2api login            browser (PKCE) login, stores tokens locally
    python -m cursor2api logout           delete the stored tokens
    python -m cursor2api status           show which credential source is in use
    python -m cursor2api chat "prompt"    one-shot chat over the raw protocol
"""
import argparse
import os
import sys

from . import auth


def cmd_serve(args):
    if args.port:
        os.environ["PORT"] = str(args.port)
    if args.bind:
        os.environ["BIND"] = args.bind
    if args.model:
        os.environ["DEFAULT_MODEL"] = args.model
    from . import server
    return server.main()


def cmd_login(args):
    auth.login(browser=not args.no_browser)
    print("Signed in. Tokens stored in %s" % auth.STORE)


def cmd_logout(args):
    print("Removed %s" % auth.STORE if auth.clear_store() else "Nothing stored")


def cmd_status(args):
    sources = []
    if os.environ.get("CURSOR_ACCESS_TOKEN"):
        sources.append("CURSOR_ACCESS_TOKEN")
    if os.environ.get("CURSOR_API_KEY"):
        sources.append("CURSOR_API_KEY")
    store = auth.load_store()
    if store.get("apiKey"):
        sources.append("stored API key")
    elif store.get("accessToken"):
        sources.append("stored OAuth session")
    if os.path.exists(auth.CLI_AUTH):
        sources.append("Cursor CLI auth file")
    print("credential file: %s" % auth.STORE)
    print("candidates: %s" % (", ".join(sources) or "none"))
    try:
        auth.access_token()
        print("access token: usable")
    except auth.AuthError as e:
        print("access token: %s" % e)
        return 1
    me = auth.whoami()
    if me:
        print("account: %s" % me.get("userEmail", me.get("email", "unknown")))
    return 0


def cmd_chat(args):
    from .session import Session
    s = Session(model=args.model or "claude-fable-5", debug=bool(os.environ.get("DBG")))
    s.start(args.prompt)
    try:
        for kind, val in s.events(idle_stop=float(args.idle)):
            if kind == "text":
                sys.stdout.write(val)
                sys.stdout.flush()
            elif kind == "thinking" and args.thinking:
                sys.stderr.write(val)
            elif kind == "error":
                print("\nerror: %s" % val, file=sys.stderr)
                return 1
        print()
        if s.usage:
            print("usage: %s" % s.usage, file=sys.stderr)
    finally:
        s.close()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="cursor2api", description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", help="run the HTTP server")
    sp.add_argument("--port", type=int)
    sp.add_argument("--bind")
    sp.add_argument("--model", help="default model")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("login", help="sign in with a browser")
    sp.add_argument("--no-browser", action="store_true",
                    help="only print the URL instead of opening it")
    sp.set_defaults(fn=cmd_login)

    sub.add_parser("logout", help="remove stored credentials").set_defaults(fn=cmd_logout)
    sub.add_parser("status", help="check credentials").set_defaults(fn=cmd_status)

    sp = sub.add_parser("chat", help="one-shot chat over the raw protocol")
    sp.add_argument("prompt")
    sp.add_argument("--model")
    sp.add_argument("--idle", default="8", help="seconds of silence before giving up")
    sp.add_argument("--thinking", action="store_true", help="print reasoning to stderr")
    sp.set_defaults(fn=cmd_chat)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 2
    try:
        return args.fn(args) or 0
    except auth.AuthError as e:
        print("auth error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
