from __future__ import annotations

import sys
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from app.api.routes import router
from app.config import LegacyDataLocationError, ensure_local_layout, load_config
from app.db.database import initialize_database

# Methods that mutate server state and therefore deserve a CSRF defense.
# Browsers send Origin on cross-origin POSTs/DELETEs, so we can verify that
# the Origin's host matches the host the browser was actually connecting to.
# That's enough to block a malicious page from triggering destructive
# endpoints while the user has the dashboard open in another tab.
_MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def _origin_is_same_host(origin: str, request_host: str | None) -> bool:
    """Return True if the Origin header refers to the same host the browser
    is connecting to.

    This is what makes Tailscale / Nebula / Cloudflare Tunnel / `tailscale
    serve` / `ssh -L`-style port forwards work: the user opens
    `http://100.100.100.100:5173/` in their phone, the browser sends
    `Origin: http://100.100.100.100:5173` *and* `Host: 100.100.100.100:5173`.
    Hostnames match → same-host traffic, allowed.

    A malicious page at `https://evil.com/` triggering a cross-origin POST
    would send `Origin: https://evil.com` and `Host: <wherever the user's
    backend is>` — those differ, so it's still blocked.
    """
    if not origin or not request_host:
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    # request_host can include a port (`100.100.100.100:5173`) or not. The
    # CSRF threat doesn't care about port — a page on the same host but a
    # different port can already poke the backend via fetch credentials,
    # so we only compare hostnames.
    request_hostname = request_host.split(":")[0]
    return parsed.hostname == request_hostname


def create_app() -> FastAPI:
    cfg = load_config()
    try:
        ensure_local_layout(cfg)
    except LegacyDataLocationError as exc:
        # Convert the upgrade-orphan-guard exception into a clean stderr
        # message + non-zero exit before FastAPI has a chance to bubble
        # a stack trace. The user needs to act (run `mm migrate-user-data`
        # or pin their old paths in local.toml) — a 30-line traceback
        # would bury the actionable parts of the message.
        # Rich Console with stderr=True for visual consistency with the
        # CLI wrapper's error formatting; falls back to plain text in
        # non-TTY contexts automatically.
        from rich.console import Console

        Console(stderr=True).print(
            "\n[red]MeetingMind cannot start: legacy data layout detected.[/red]\n"
            f"{exc}\n"
        )
        sys.exit(2)
    initialize_database(cfg.paths.database_path)
    app = FastAPI(title="MeetingMind", version="0.1.0")

    loopback_origins = {
        f"http://localhost:{cfg.runtime.dashboard_port}",
        f"http://127.0.0.1:{cfg.runtime.dashboard_port}",
        # CLI tools / direct backend hits use the backend port as the origin.
        f"http://localhost:{cfg.runtime.backend_port}",
        f"http://127.0.0.1:{cfg.runtime.backend_port}",
    }

    # CORS allow-origin: restrict to loopback + private/CGNAT ranges only.
    # Audit finding H-1: the previous regex (`[A-Za-z0-9._\-]+`) matched
    # *any* public domain, so e.g. evil.com could read `/api/meetings`
    # cross-origin. Even though we don't use cookies, allow_credentials=True
    # combined with the broad regex made same-origin policy unusually
    # permissive for a local-first app.
    #
    # New ranges allowed:
    #   - localhost, 127.0.0.1 (loopback)
    #   - 100.64.0.0/10        (Tailscale CGNAT — `100.[64-127]`)
    #   - 10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12  (RFC1918 / private LAN)
    # Public domains are intentionally NOT matched. If a user puts MeetingMind
    # behind a real domain (Cloudflare Tunnel, etc.), they will need to
    # explicitly add the host — the same-host CSRF middleware below will
    # still permit it for mutations, but cross-origin GETs from random sites
    # are now blocked at the CORS layer.
    #
    # allow_credentials=False: we have no cookies or session auth, so
    # browsers don't need to attach credentials cross-origin. Setting this
    # false also lets the browser enforce strict CORS rules.
    # v0.2.11: when a user runs MeetingMind behind `tailscale serve`, the
    # browser sends Origin headers like
    # `https://your-machine.tailXXXXXX.ts.net:8443` — a MagicDNS
    # hostname rather than the raw Tailscale 100.x CGNAT IP. The regex
    # needs to whitelist `*.ts.net` (and the older `*.tailscale-relay.com`
    # ingress hostname) alongside the IP ranges, otherwise the CORS
    # middleware silently rejects the request and the mobile dashboard
    # sees an empty meetings list with no obvious error.
    _PRIVATE_HOST_RE = (
        r"https?://("
        r"localhost|127\.0\.0\.1|"
        r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}|"
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
        # Tailscale hostnames carry a tailnet identifier as a label, so
        # the host is `<machine>.<tailnet>.ts.net` (three+ labels).
        # Match any number of dot-separated labels before the suffix.
        r"(?:[A-Za-z0-9\-]+\.)+ts\.net|"
        r"(?:[A-Za-z0-9\-]+\.)+tailscale-relay\.com"
        r")(?::\d+)?"
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_PRIVATE_HOST_RE,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Pre-compile a re-usable matcher for the same private-host regex
    # the CORS layer uses, so the CSRF check can fast-allow Tailscale
    # MagicDNS origins even when the Vite proxy or `tailscale serve`
    # rewrites the Host header to `127.0.0.1:5173`. The previous
    # design only allowed cross-origin mutations when Origin host ==
    # Request host — which fails when a proxy in the middle rewrites
    # Host. The result: mobile clients hitting Tailscale got 403 on
    # the very first POST /api/install during app init, never
    # populated meetings.
    import re as _re

    _origin_host_allowed = _re.compile(_PRIVATE_HOST_RE)

    @app.middleware("http")
    async def block_cross_origin_mutations(request: Request, call_next):  # type: ignore[no-untyped-def]
        # CSRF defense for mutating requests:
        # 1. No Origin header → non-browser caller (curl, CLI, native test), allow.
        # 2. Origin in the explicit loopback allowlist → allow.
        # 3. Origin host matches the request's own Host header → same-host
        #    traffic over a tunnel, allow.
        # 4. Origin matches the private-host regex (Tailscale CGNAT,
        #    *.ts.net MagicDNS, RFC1918 LAN ranges) → allow even when
        #    a proxy rewrote the Host header.
        # 5. Otherwise → block. A malicious page can't forge Origin,
        #    and a public-internet host won't match the regex.
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("origin")
            if origin and origin not in loopback_origins:
                request_host = request.headers.get("host")
                if not _origin_is_same_host(
                    origin, request_host
                ) and not _origin_host_allowed.fullmatch(origin):
                    # Tailscale / private-LAN check by Origin alone —
                    # Host may have been rewritten by an intermediary.
                    return JSONResponse(
                        {"detail": "cross_origin_mutation_blocked"},
                        status_code=403,
                    )
        return await call_next(request)

    # Any API call that re-enters `ensure_local_layout` post-startup (e.g.
    # a settings-mutation endpoint that flushes layout after rewriting
    # paths) returns a clean 503 with the recovery instructions instead
    # of bubbling a 500 with the bare exception. The startup path itself
    # is guarded above and exits the process; this handler is the
    # post-startup safety net.
    @app.exception_handler(LegacyDataLocationError)
    async def _legacy_data_location_handler(
        request: Request, exc: LegacyDataLocationError
    ) -> JSONResponse:
        return JSONResponse(
            {
                "detail": "legacy_data_location",
                "message": str(exc),
                "recover_with": "mm migrate-user-data",
            },
            status_code=503,
        )

    app.include_router(router, prefix="/api")
    return app


app = create_app()
