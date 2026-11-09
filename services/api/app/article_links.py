"""Credential-free article links for the owned local library."""
import base64
import hashlib
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .config import settings
from .models import LocalInstallation


router = APIRouter()


def local_library_id(db, owner_id):
    installation = db.get(LocalInstallation, 1)
    if installation is None or installation.owner_id != owner_id:
        return None
    return str(UUID(installation.library_id))


def article_url(db, source):
    library_id = local_library_id(db, source.owner_id)
    if library_id is None:
        return None
    return f'knowledge-inbox://library/{library_id}/source/{UUID(source.id)}'


def browser_article_url(address: str, app_url: str | None) -> str | None:
    """Use only the exact owned article URI, never an arbitrary redirect target."""
    if not isinstance(app_url, str):
        return None
    try:
        parsed = urlsplit(app_url)
    except ValueError:
        return None
    parts = parsed.path.split('/')
    if (parsed.scheme != 'knowledge-inbox' or parsed.netloc != 'library'
            or parsed.query or parsed.fragment or len(parts) != 4 or parts[2] != 'source'):
        return None
    try:
        library_id, source_id = str(UUID(parts[1])), str(UUID(parts[3]))
    except ValueError:
        return None
    if app_url != f'knowledge-inbox://library/{library_id}/source/{source_id}':
        return None
    return f'{address}/open/library/{library_id}/source/{source_id}'


_OPEN_SCRIPT = "window.location.replace(document.getElementById('open-article').href);"


@router.get('/open/library/{library_id}/source/{source_id}', response_class=HTMLResponse,
            include_in_schema=False)
def open_article(library_id: UUID, source_id: UUID, request: Request):
    # This page holds no article data and grants no API access. The native client
    # still validates its active library and reads the article with app auth.
    if (settings.environment != 'local' or request.client is None
            or request.client.host not in {'127.0.0.1', '::1'}
            or request.url.hostname not in {'127.0.0.1', '::1'} or request.url.query):
        raise HTTPException(status_code=404)
    target = f'knowledge-inbox://library/{library_id}/source/{source_id}'
    script_hash = base64.b64encode(hashlib.sha256(_OPEN_SCRIPT.encode()).digest()).decode()
    return HTMLResponse(f'''<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>打开知识库文章</title><style>
body{{font:16px -apple-system,BlinkMacSystemFont,sans-serif;background:#171b20;color:#e6e7e9;
margin:0;min-height:100vh;display:grid;place-items:center}}main{{max-width:28rem;padding:2rem}}
h1{{font-size:24px}}p{{color:#aeb3ba;line-height:1.7}}a{{display:inline-block;padding:12px 18px;
border-radius:8px;background:#b78354;color:#fff;text-decoration:none}}
</style></head><body><main><h1>打开知识库文章</h1>
<p>正在尝试打开 Knora。浏览器如有提示，请允许打开应用。</p>
<a id="open-article" href="{target}">在 Knora 中打开</a>
<p>如果没有自动打开，请点击上方按钮，并保持 Knora 运行。</p>
</main><script>{_OPEN_SCRIPT}</script></body></html>''', headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'sha256-{script_hash}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    })
