"""Themed Burr session UI, served without forking Burr's frontend.

The ``ui`` CLI command copies Burr's prebuilt static UI, patches ``index.html``
with a co-brand bar and a theme stylesheet, hides Burr's own left sidebar, and
serves the result alongside Burr's API routes (``create_burr_ui_app(serve_static
=False)``). The brand bar reads the CLI's name, so a rebranded distribution
(``coffee-agent ui``) shows its own name with a "powered by Theodosia" credit,
while Theodosia's own CLI shows the ⊢ mark.

The store the UI reads is set through Burr's ``burr_path`` env var, which must be
in the environment before ``burr.tracking.server.run`` is imported (it builds its
backend at module load). ``serve_themed`` sets it first, then imports.
"""

from __future__ import annotations

import html as _html
import os
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from theodosia.tokens import LIGHT as _TOKENS
from theodosia.tokens import as_css_variables as _to_css

_BURR_REPO = "https://github.com/apache/burr"

# Co-brand bar + sidebar removal. Layout-only rules live here; every color and
# font token is prepended from theodosia.tokens.LIGHT so this surface, the docs
# site, and the TUI stay in lockstep.
_THEME_CSS = (
    (_to_css(_TOKENS) + "\n")
    + """\
:root {
  --thd-canvas-grid: radial-gradient(circle at 1px 1px, rgba(10,10,10,0.04) 1px, transparent 0);
}

html, body { height: 100%; }
body {
  display: flex !important;
  flex-direction: column;
  background: var(--thd-paper) !important;
  background-image: var(--thd-canvas-grid) !important;
  background-size: 24px 24px !important;
  background-attachment: fixed !important;
  color: var(--thd-subtle) !important;
  font-family: var(--thd-sans) !important;
}

:where(a) { color: var(--thd-pine); }
:where(a:hover) { color: var(--thd-pine-soft); }
:where(tbody tr):hover { background: var(--thd-pine-tint) !important; }
:where(code, kbd) {
  font-family: var(--thd-mono);
  background: var(--thd-pine-tint);
  border-radius: 6px;
  padding: 1px 5px;
  font-size: 0.9em;
}

#thd-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 18px;
  background: var(--thd-sheet);
  border-bottom: 1px solid var(--thd-edge);
  color: var(--thd-ink);
  font-family: var(--thd-sans);
  font-size: 13px;
  position: relative;
  z-index: 9999;
}
#thd-bar .thd-mark {
  font-family: var(--thd-display);
  font-size: 19px;
  line-height: 1;
  color: var(--thd-pine);
}
#thd-bar .thd-name {
  font-family: var(--thd-display);
  font-weight: 700;
  font-size: 17px;
  color: var(--thd-pine);
  letter-spacing: -0.005em;
}
#thd-bar .thd-sub {
  color: var(--thd-muted);
  font-weight: 400;
  letter-spacing: 0.01em;
}
#thd-bar .thd-spacer { flex: 1 1 auto; }
#thd-bar .thd-credit {
  color: var(--thd-muted);
  font-size: 12px;
  letter-spacing: 0.01em;
}
#thd-bar .thd-credit a { color: var(--thd-pine); text-decoration: none; }
#thd-bar .thd-credit a:hover { color: var(--thd-pine-soft); text-decoration: underline; }

/* Burr's sidebar uses hashed component class names but textually stable Tailwind
   utilities. Target the specific combo seen in the build to avoid hitting
   unrelated fixed elements. The JS sweep below is the fallback when the build's
   class names drift. */
[class*="lg:fixed"][class*="lg:inset-y-0"],
[class*="lg:w-72"],
aside,
[data-testid*="sidebar" i],
[data-sidebar] { display: none !important; }

button[aria-label*="toggle" i][aria-label*="sidebar" i],
button[aria-label*="navigation" i],
[aria-label*="open sidebar" i],
[aria-label*="close sidebar" i] { display: none !important; }

a[href*="discord.gg"],
a[href$="/examples"],
a[href*="/examples/"] { display: none !important; }

#root {
  flex: 1 1 auto;
  min-height: 0;
  padding-inline: 32px;
  padding-top: 16px;
}
"""
)

# Burr's sidebar carries hashed Tailwind class names that CSS can't reliably
# pin, so detect it by shape after React renders (anchored left, tall, narrow,
# fixed/absolute) and zero the leftover gutter up the ancestor tree. Runs on a
# few timers and on DOM mutations.
_KILL_SIDEBAR_JS = r"""
(function () {
  function isSidebar(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.id === 'thd-bar' || (el.closest && el.closest('#thd-bar'))) return false;
    if (el.dataset && el.dataset.thdHidden === '1') return false;
    var cs = window.getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'absolute'
        && cs.position !== 'sticky') return false;
    var rect = el.getBoundingClientRect();
    if (rect.left > 80) return false;
    if (rect.width > 420 || rect.width < 32) return false;
    if (rect.height < window.innerHeight * 0.5) return false;
    return true;
  }
  function findMainContent() {
    var root = document.getElementById('root');
    if (!root) return null;
    var stack = [root];
    while (stack.length) {
      var el = stack.shift();
      if (!el || el.nodeType !== 1) continue;
      if (el !== root) {
        if (el.id !== 'thd-bar' && !(el.dataset && el.dataset.thdHidden === '1')) {
          var cs = window.getComputedStyle(el);
          if (cs.display !== 'none' && cs.visibility !== 'hidden') {
            var rect = el.getBoundingClientRect();
            if (rect.left > 50 && rect.width > 300) return el;
          }
        }
      }
      for (var i = 0; i < el.children.length; i++) stack.push(el.children[i]);
    }
    return null;
  }
  function fixGutter() {
    var target = findMainContent();
    if (!target) return 0;
    var fixed = 0;
    for (var node = target; node && node !== document.body; node = node.parentElement) {
      if (node.id === 'root') continue;
      if (node.dataset && node.dataset.thdGutterFixed === '1') continue;
      var cs = window.getComputedStyle(node);
      var ml = parseFloat(cs.marginLeft || '0');
      var pl = parseFloat(cs.paddingLeft || '0');
      var lf = parseFloat(cs.left || '0');
      var touched = false;
      if (ml > 30) { node.style.setProperty('margin-left', '0', 'important'); touched = true; }
      if (pl > 30) { node.style.setProperty('padding-left', '0', 'important'); touched = true; }
      if (lf > 30) { node.style.setProperty('left', '0', 'important'); touched = true; }
      var gtc = cs.gridTemplateColumns || '';
      if (/^\s*\d+px\s/.test(gtc)) {
        node.style.setProperty('grid-template-columns', '1fr', 'important');
        touched = true;
      }
      if (touched) { node.dataset.thdGutterFixed = '1'; fixed++; }
    }
    return fixed;
  }
  function sweep() {
    var nodes = document.body.querySelectorAll('*');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (isSidebar(el)) {
        el.style.setProperty('display', 'none', 'important');
        el.dataset.thdHidden = '1';
      }
    }
    fixGutter();
  }
  var obs = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      if (muts[i].addedNodes && muts[i].addedNodes.length) { sweep(); return; }
    }
  });
  if (document.body) obs.observe(document.body, { childList: true, subtree: true });
  [50, 250, 800, 2000].forEach(function (t) { setTimeout(sweep, t); });
})();
"""

_FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Inria+Serif:wght@400;700&"
    "family=Funnel+Sans:wght@400;500;700&"
    'family=JetBrains+Mono:wght@400;500&display=swap">'
)


def burr_credit_html(*, powered_by_theodosia: bool) -> str:
    """Right-side credit string for the brand bar.

    Always attributes the UI to Apache Burr (it is Burr's frontend, reskinned).
    A rebranded distribution also credits Theodosia as the platform.
    """
    burr = f'UI by <a href="{_BURR_REPO}" target="_blank" rel="noopener">Apache Burr</a>'
    return f"powered by Theodosia &middot; {burr}" if powered_by_theodosia else burr


def brand_identity(
    prog_name: str, *, ui_title: str | None = None, ui_mark: str | None = None
) -> tuple[str, str | None, str]:
    """Resolve the brand bar's (name, mark, credit_html) for a CLI.

    Theodosia's own CLI gets the ⊢ mark and no platform credit; a rebrand gets
    its capitalized ``prog_name`` (unless ``ui_title`` overrides), no mark unless
    it sets one, and a "powered by Theodosia" credit.
    """
    is_theodosia = prog_name == "theodosia"
    name = ui_title or (prog_name[:1].upper() + prog_name[1:])
    mark = ui_mark or ("⊢" if is_theodosia else None)
    credit = burr_credit_html(powered_by_theodosia=not is_theodosia)
    return name, mark, credit


def _bar_html(*, name: str, mark: str | None, subtitle: str, credit_html: str) -> str:
    mark_span = f'<span class="thd-mark">{_html.escape(mark)}</span>' if mark else ""
    return (
        '<div id="thd-bar">'
        f"{mark_span}"
        f'<span class="thd-name">{_html.escape(name)}</span>'
        f'<span class="thd-sub">{_html.escape(subtitle)}</span>'
        '<span class="thd-spacer"></span>'
        f'<span class="thd-credit">{credit_html}</span>'
        "</div>"
    )


def build_themed_assets(
    dest: Path,
    *,
    name: str,
    mark: str | None,
    subtitle: str,
    credit_html: str,
) -> Path:
    """Copy Burr's static UI to ``dest`` and patch it for ``name``.

    Returns the build directory. Rebuilt from scratch each call so brand or
    token changes always take effect.
    """
    src = Path(str(files("burr").joinpath("tracking/server/build")))
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    css_dir = dest / "static" / "css"
    css_dir.mkdir(parents=True, exist_ok=True)
    (css_dir / "theodosia-theme.css").write_text(_THEME_CSS)

    index = dest / "index.html"
    page = index.read_text()
    title = _html.escape(f"{name} · Session Console")
    page = page.replace("<title>Burr</title>", f"<title>{title}</title>")
    if "theodosia-theme.css" not in page:
        page = page.replace(
            "</head>",
            f'{_FONTS_LINK}<link rel="stylesheet" href="/static/css/theodosia-theme.css"></head>',
        )
    if 'id="thd-bar"' not in page:
        bar = _bar_html(name=name, mark=mark, subtitle=subtitle, credit_html=credit_html)
        page = page.replace('<div id="root">', f'{bar}<div id="root">')
    if "theodosia-kill-sidebar" not in page:
        page = page.replace(
            "</body>",
            f'<script id="theodosia-kill-sidebar">{_KILL_SIDEBAR_JS}</script></body>',
        )
    index.write_text(page)

    manifest = dest / "manifest.json"
    if manifest.exists():
        import json

        data = json.loads(manifest.read_text())
        data["short_name"] = name
        data["name"] = f"{name} Session Console"
        manifest.write_text(json.dumps(data, indent=2))

    return dest


def build_app(build_dir: Path) -> Any:
    """Burr's API routes + the themed static assets, served at the root path.

    ``burr_path`` must already be set in the environment: importing
    ``burr.tracking.server.run`` builds Burr's backend at module load.
    """
    from burr.tracking.server.run import create_burr_ui_app
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.responses import FileResponse

    app = create_burr_ui_app(serve_static=False)
    app.mount("/static", StaticFiles(directory=str(build_dir / "static")), name="static")
    index_html = (build_dir / "index.html").read_text()

    @app.get("/manifest.json")
    async def manifest_json() -> Any:
        return FileResponse(
            str(build_dir / "manifest.json"), media_type="application/manifest+json"
        )

    @app.get("/favicon.ico")
    async def favicon() -> Any:
        return FileResponse(str(build_dir / "favicon.ico"), media_type="image/x-icon")

    # Catch-all for client-side routes (/project/...): serve the patched index
    # and let React Router take over. Registered last, so the API routes win.
    # No Request param: with `from __future__ import annotations`, FastAPI can't
    # resolve a function-local ``Request`` annotation and would treat it as a
    # query field. The handler doesn't need the request anyway.
    @app.get("/{rest_of_path:path}")
    async def react_app(rest_of_path: str) -> Any:
        return HTMLResponse(index_html)

    return app


def serve_themed(
    *,
    host: str,
    port: int,
    storage_dir: Path,
    name: str,
    mark: str | None,
    subtitle: str,
    credit_html: str,
    open_browser: bool,
) -> None:
    """Build the themed assets and serve them, reading sessions from ``storage_dir``."""
    # Lowercase is required: Burr's backend settings read env_prefix "burr_"
    # + field "path", i.e. the literal "burr_path". BURR_PATH would be ignored.
    os.environ["burr_path"] = str(Path(storage_dir).expanduser())  # noqa: SIM112

    build_dir = Path(tempfile.gettempdir()) / f"theodosia-ui-{name}" / "build"
    build_themed_assets(build_dir, name=name, mark=mark, subtitle=subtitle, credit_html=credit_html)

    app = build_app(build_dir)

    if open_browser:
        import threading
        import webbrowser

        url = f"http://{host}:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
