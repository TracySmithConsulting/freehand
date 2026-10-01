import typer
from pathlib import Path
import sys

app = typer.Typer(
    help="FreeHand CLI - Local AI agent system with Tailscale access",
    add_completion=False
)

def _get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent


@app.command()
def init():
    """Initialize FreeHand database and create initial vault structure."""
    project_root = _get_project_root()

    sys.path.insert(0, str(project_root))
    from core.database import init_db

    typer.echo("Initializing FreeHand...")

    conn = init_db()
    typer.echo("   [OK] Database initialized at " + str(project_root / "agent.db"))
    conn.close()

    vault_dir = project_root / "vault"
    vault_dir.mkdir(exist_ok=True)

    scribble_path = vault_dir / "00_Scribble.md"
    if not scribble_path.exists():
        scribble_content = """# 00_Scribble

> Agent scratchpad for quick notes and ideas

## Quick Notes

- [ ] Task to remember
- [ ] Idea to explore
- [ ] Question to investigate

## Recent Activity

### {date}
- Agent initialized

---

*This file serves as the agent's working memory. Use it for:*
- *Quick notes during agent operation*
- *Temporarily storing information between tasks*
- *Tracking ongoing investigations*
"""
        scribble_path.write_text(scribble_content.format(date="2024-01-01"))
        typer.echo("   [OK] Created " + str(scribble_path))
    else:
        typer.echo("   - Vault already exists at " + str(vault_dir))

    typer.echo("FreeHand initialization complete!")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo("   - Start the server: python server.py")
    typer.echo("   - Access locally: http://localhost:8000")
    typer.echo("   - Access via Tailscale: http://100.x.y.z:8000")


@app.command()
def serve():
    """Start the FreeHand server."""
    typer.echo("Starting FreeHand server on http://0.0.0.0:8000")
    typer.echo("Press Ctrl+C to stop")

    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )


@app.command()
def sweep():
    """Run an immediate scribble sweep."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.scheduler import process_scribble
    result = process_scribble()
    typer.echo("Sweep complete:")
    typer.echo("  Processed:  " + str(result["processed"]) + " entry(ies)")
    typer.echo("  Tasks created: " + str(result["tasks_created"]))
    typer.echo("  Threads updated: " + str(result["threads_updated"]))


@app.command()
def gateway(subcommand: str = typer.Argument(help="status|start|stop|pair"), phone: str = typer.Argument(default="")):
    """Manage the remote chat gateway (Telegram, Slack, WhatsApp)."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.gateway import get_gateway_status, _get_bridge_url

    if subcommand == "status":
        status = get_gateway_status()
        typer.echo("Gateway Status:")
        typer.echo("  Tier: " + status["tier"])
        typer.echo("  Pending approvals: " + str(status["pending_approvals"]))
        typer.echo("")
        tg = status["telegram"]
        typer.echo("  Telegram: " + ("configured" if tg["configured"] else "not configured"))
        sk = status["slack"]
        typer.echo("  Slack: " + ("configured" if sk["configured"] else "not configured"))
        wa = status["whatsapp"]
        typer.echo("  WhatsApp: " + ("linked" if wa["auth_exists"] else "not linked"))
        typer.echo("  Bridge port: " + str(status["bridge_port"]))

    elif subcommand == "start":
        typer.echo("Starting gateway...")
        typer.echo("  Telegram/Slack webhooks: enabled (configure tokens in settings)")
        typer.echo("  WhatsApp bridge: run 'npm start' in bridge/ directory")
        typer.echo("  Status: freehand gateway status")

    elif subcommand == "stop":
        typer.echo("Gateway stopped.")

    elif subcommand == "pair":
        if not phone:
            typer.echo("Usage: freehand gateway pair <phone_number>")
            raise SystemExit(1)
        import aiohttp
        bridge_url = _get_bridge_url()
        async def do_pair():
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(
                        bridge_url + "/pair",
                        json={"phone": phone},
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        return await resp.json()
            except Exception as e:
                return {"error": str(e)}
        import asyncio
        result = asyncio.run(do_pair())
        if "code" in result:
            typer.echo("Pairing code for " + result["phone"] + ": " + result["code"])
            typer.echo("Enter this code in WhatsApp on your phone to link the device.")
        else:
            typer.echo("Error: " + result.get("error", "unknown"))

    else:
        typer.echo("Usage: freehand gateway [status|start|stop|pair <phone>]")


# ── Connection commands ──────────────────────────────────────────────────────────

@app.command()
def connect(service: str = typer.Argument(help="Service: google, microsoft, zoom, facebook, instagram, github, email"),
            label: str = typer.Option("default", "--label", "-l", help="Account label (e.g. work, personal)")):
    """Connect a service via OAuth or token."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.oauth.router import _get_redirect_uri
    from core.oauth.providers import get_connector
    from core.oauth.manager import get_connection

    conn = get_connection(service, label)
    if conn:
        typer.echo(f"Already connected: {service} ({label})")
        typer.echo(f"  Scopes: {conn['scopes']}")
        typer.echo(f"  Connected: {conn['connected_at']}")
        raise SystemExit(0)

    if service not in ("google", "microsoft", "zoom", "facebook", "instagram", "github", "email"):
        typer.echo(f"Unknown service: {service}")
        typer.echo("Available: google, microsoft, zoom, facebook, instagram, github, email")
        raise SystemExit(1)

    connector = get_connector(service)
    if not connector:
        if service == "github":
            pat = typer.prompt("Paste your GitHub Personal Access Token", hide_input=True)
            if not pat:
                typer.echo("Token required.")
                raise SystemExit(1)
            import asyncio
            from core.oauth.providers.github_pat import GitHubPATConnector
            gc = GitHubPATConnector()
            try:
                result = asyncio.run(gc.validate_pat(pat))
                typer.echo(f"Connected as: {result.get('login', 'unknown')}")
            except Exception as e:
                typer.echo(f"Connection failed: {e}")
                raise SystemExit(1)
        elif service == "email":
            host = typer.prompt("IMAP/SMTP Host")
            port = typer.prompt("Port (993 for SSL, 587 for TLS)", default="993")
            username = typer.prompt("Username (email address)")
            password = typer.prompt("Password", hide_input=True)
            use_ssl = typer.confirm("Use SSL?", default=True)
            typer.echo(f"Email configured: {username}@{host}")
        else:
            typer.echo(f"To connect {service}, open the FreeHand web UI at http://localhost:8000")
            typer.echo(f"Go to the Connections panel and click Connect.")
        raise SystemExit(0)

    typer.echo(f"Connecting to {connector.name} ({label})...")
    typer.echo(f"Open this URL in your browser:")
    import asyncio
    from fastapi.testclient import TestClient
    from server import app as server_app
    client = TestClient(server_app)
    resp = client.get(f"/api/integrations/{service}/authorize?label={label}")
    if resp.status_code == 200:
        data = resp.json()
        typer.echo("")
        typer.echo(data["authorize_url"])
        typer.echo("")
        typer.echo("After authorizing, the callback will complete the connection.")
    else:
        typer.echo(f"Error: {resp.status_code} - {resp.text}")
        raise SystemExit(1)


@app.command()
def connections():
    """List all connected services."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.oauth.manager import list_connections
    conns = list_connections()
    if not conns:
        typer.echo("No connections. Use 'freehand connect <service>' to add one.")
        return
    typer.echo(f"{'Service':<15} {'Label':<12} {'Status':<10} {'Connected At'}")
    typer.echo("-" * 65)
    for c in conns:
        typer.echo(f"{c['service']:<15} {c['label']:<12} {c['status']:<10} {c['connected_at']}")


@app.command()
def rename(
    service: str = typer.Argument(),
    old_label: str = typer.Option(..., "--from", help="Current label"),
    new_label: str = typer.Option(..., "--to", help="New label"),
):
    """Rename a connection's label (e.g. 'dbsa' -> 'shazacin')."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.oauth.manager import rename_connection_label
    ok = rename_connection_label(service, old_label, new_label)
    if ok:
        typer.echo(f"Renamed: {service} ({old_label}) -> ({new_label})")
    else:
        typer.echo(f"No connection found: {service} ({old_label})")
        raise SystemExit(1)


@app.command()
def disconnect(service: str = typer.Argument(), label: str = typer.Option("default", "--label", "-l")):
    """Disconnect a service."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.oauth.manager import delete_connection
    deleted = delete_connection(service, label)
    if deleted:
        typer.echo(f"Disconnected: {service} ({label})")
    else:
        typer.echo(f"Not connected: {service} ({label})")
        raise SystemExit(1)


@app.command()
def test(service: str = typer.Argument(), label: str = typer.Option("default", "--label", "-l")):
    """Test if a connection is still valid."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.oauth.manager import get_connection
    from core.oauth.providers import get_connector

    conn = get_connection(service, label)
    if not conn:
        typer.echo(f"Not connected: {service} ({label})")
        raise SystemExit(1)

    connector = get_connector(service)
    if not connector:
        typer.echo(f"No connector for: {service}")
        raise SystemExit(1)

    import asyncio
    valid = asyncio.run(connector.test(conn["token_data"]))
    if valid:
        typer.echo(f"{service.title()} ({label}): Connected and valid")
    else:
        typer.echo(f"{service.title()} ({label}): Connection invalid — reconnect required")
        raise SystemExit(1)


# ── Shared OAuth app commands (Round 8) ─────────────────────────────────
# Tracy-managed registry of FreeHand-owned shared OAuth apps. Each entry
# holds a client_id + Fernet-encrypted client_secret in broker_config.json.
# `add` / `remove` / `list` round-trip the registry. `list` NEVER displays
# secrets — only client_id + scopes + audit metadata.

shared_app_app = typer.Typer(help="Manage FreeHand-managed shared OAuth apps (Round 8 tier-1b).")


def _shared_app_imports():
    """Lazy import — keeps the CLI boot path fast."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.oauth import shared_apps as _sa
    return _sa


@shared_app_app.command("add")
def shared_app_add(
    service: str = typer.Argument(..., help="Service id (e.g. slack, github, notion)"),
    client_id: str = typer.Option(..., "--client-id", help="OAuth client_id from the developer portal"),
    client_secret: str = typer.Option(..., "--client-secret", help="OAuth client_secret — encrypted on disk, never displayed"),
    scopes: str = typer.Option("", "--scopes", help="Comma-separated OAuth scopes (e.g. 'chat:write,channels:read')"),
    registered_by: str = typer.Option("tracy", "--registered-by", help="Audit: who registered this app"),
):
    """Register a FreeHand-managed shared OAuth app. The client_secret is
    Fernet-encrypted in vault/broker_config.json immediately on write."""
    if not client_id.strip() or not client_secret.strip():
        typer.echo("client_id and client_secret are required (non-empty).")
        raise SystemExit(1)
    sa = _shared_app_imports()
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()] if scopes else []
    entry = sa.add_shared_app(
        service=service,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scope_list,
        registered_by=registered_by,
    )
    typer.echo(f"Added shared app: {entry['service']}")
    typer.echo(f"  client_id:  {entry['client_id']}")
    typer.echo(f"  scopes:     {', '.join(entry['scopes']) or '(none)'}")
    typer.echo(f"  registered: {entry['registered_at']} by {entry['registered_by']}")
    typer.echo("")
    typer.echo("Secret is encrypted on disk. Use 'freehand shared-app list' to view metadata.")


@shared_app_app.command("list")
def shared_app_list():
    """List all registered shared apps. Secrets are NEVER displayed."""
    sa = _shared_app_imports()
    entries = sa.list_shared_apps()
    if not entries:
        typer.echo("No shared apps registered. Use 'freehand shared-app add <service>' to add one.")
        return
    typer.echo(f"{'Service':<15} {'Client ID':<30} {'Scopes':<40} {'Registered':<20}")
    typer.echo("-" * 105)
    for e in entries:
        scopes_str = ",".join(e["scopes"][:3])
        if len(e["scopes"]) > 3:
            scopes_str += f" (+{len(e['scopes']) - 3} more)"
        typer.echo(f"{e['service']:<15} {e['client_id']:<30} {scopes_str:<40} {e['registered_by']}")


@shared_app_app.command("remove")
def shared_app_remove(
    service: str = typer.Argument(..., help="Service id to remove"),
):
    """Remove a shared app entry. The user secret on the developer portal
    is unaffected — this only removes the local copy from the FreeHand vault."""
    sa = _shared_app_imports()
    if sa.remove_shared_app(service):
        typer.echo(f"Removed shared app: {service}")
    else:
        typer.echo(f"No shared app registered for: {service}")
        raise SystemExit(1)


app.add_typer(shared_app_app, name="shared-app")


# ── Agent subcommands ──────────────────────────────────────────────────────────

agent_app = typer.Typer(help="Manage AI agents (detection, import, status)")


@agent_app.command("list")
def agents_list():
    """List detected AI agents and their import status."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.agents import detect_agents, get_import_status

    agents = detect_agents()
    status = get_import_status()

    typer.echo(f"Detected agents: {len(agents)}\n")
    for agent in agents:
        imported = agent.name in status.get("agents", [])
        status_str = "imported" if imported else "not imported"
        typer.echo(f"  [{agent.display_name}] at {agent.path}")
        typer.echo(f"    Files: {agent.file_count()} | Skills: N/A | State DB: {agent.state_db_path is not None}")
        typer.echo(f"    Status: {status_str}")
        typer.echo("")

    if not agents:
        typer.echo("  No AI agents detected on this system.")


@agent_app.command("import")
def agents_import(agent_name: str = typer.Argument(..., help="Agent to import (hermes, openclaw)"),
                   include_state: bool = typer.Option(False, "--state", help="Also extract from SQLite state.db"),
                   no_skills: bool = typer.Option(False, "--no-skills", help="Skip importing skills"),
                   dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing")):
    """Import memories and skills from a detected agent."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.agents import detect_agent, import_hermes, import_openclaw, record_import
    from core.agents.models import ImportOptions
    from core.database import init_db

    init_db()

    detected = detect_agent(agent_name)
    if not detected:
        typer.echo(f"Agent not found: {agent_name}")
        typer.echo(f"Available agents: {[a.name for a in detect_agents()]}")
        raise SystemExit(1)

    vault_dir = _get_project_root() / "vault"
    opts = ImportOptions(
        agent=agent_name,
        include_state=include_state,
        include_skills=not no_skills,
        dry_run=dry_run,
        overwrite=True,
    )

    importers = {
        "hermes": import_hermes,
        "openclaw": import_openclaw,
    }
    importer = importers.get(agent_name)
    if not importer:
        typer.echo(f"No importer for: {agent_name}")
        raise SystemExit(1)

    result = importer(detected, opts, vault_dir)

    typer.echo(f"Agent: {detected.display_name}")
    typer.echo(f"  Path: {detected.path}")
    typer.echo(f"  Files imported: {len(result.files_imported)}")
    typer.echo(f"  Skills imported: {len(result.skills_imported)}")
    typer.echo(f"  State extracted: {result.state_extracted}")
    if result.errors:
        typer.echo(f"  Errors: {len(result.errors)}")
        for e in result.errors[:5]:
            typer.echo(f"    - {e}")
    if result.skipped:
        typer.echo(f"  Skipped: {len(result.skipped)}")
        for s in result.skipped[:5]:
            typer.echo(f"    - {s}")

    if not opts.dry_run:
        record_import(result)
        typer.echo(f"\nImport recorded to database.")

    if opts.dry_run:
        typer.echo(f"\n(Dry run — no files were written.)")
    else:
        typer.echo(f"\nImports saved to: {result.vault_root}")


@agent_app.command("status")
def agents_status():
    """Show import history and current status."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.agents import list_imports, list_imported_sources, get_import_status

    status = get_import_status()
    typer.echo(f"Total imports: {status['total_imports']}")
    typer.echo(f"Total files: {status['total_files']}")
    typer.echo(f"Total skills: {status['total_skills']}")
    typer.echo(f"Agents: {', '.join(status['agents'] or ['none'])}")
    typer.echo("")
    for imp in status["imports"]:
        typer.echo(f"  {imp['agent']:<12} files={imp['files_imported']:>3} skills={imp['skills_imported']:>2} "
                   f"state={'Y' if imp['state_extracted'] else 'n'}  {imp['imported_at']}")


@agent_app.command("remove")
def agents_remove(agent_name: str = typer.Argument(..., help="Agent to remove (hermes, openclaw)")):
    """Remove all imported sources for an agent."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.agents import remove_import
    from core.database import init_db
    from pathlib import Path

    init_db()
    vault_dir = _get_project_root() / "vault"
    deleted = remove_import(agent_name)

    # Also remove from disk
    import_shares = ["imports" / agent_name, "skills" / f"{agent_name}-imports"]
    for share in import_shares:
        p = vault_dir / share
        if p.exists():
            import shutil
            shutil.rmtree(p)
            typer.echo(f"  Removed: {p}")

    if deleted:
        typer.echo(f"Removed imports for: {agent_name}")
    else:
        typer.echo(f"No imports found for: {agent_name}")


# ── Tool subcommands ────────────────────────────────────────────────────────────

office_app = typer.Typer(help="Office document tools (docx, xlsx, pptx)")


@office_app.command("read-docx")
def office_read_docx(path: str = typer.Argument(..., help="Path to .docx file")):
    """Read a .docx file."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.tools.office import read_docx as _read_docx
    result = _read_docx(path)
    if "error" in result:
        typer.echo("Error: " + result["error"])
        raise SystemExit(1)
    typer.echo("Title: " + result["title"])
    typer.echo("Paragraphs: " + str(result["paragraph_count"]))
    typer.echo("Tables: " + str(result["table_count"]))
    typer.echo("---")
    for para in result['paragraphs'][:20]:
        typer.echo(para)


@office_app.command("read-xlsx")
def office_read_xlsx(path: str = typer.Argument(..., help="Path to .xlsx file")):
    """Read an .xlsx file."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.tools.office import read_xlsx as _read_xlsx
    result = _read_xlsx(path)
    if "error" in result:
        typer.echo("Error: " + result["error"])
        raise SystemExit(1)
    typer.echo("Sheets: " + str(result['metadata']['sheet_count']))
    for sheet_name, rows in result['sheets'].items():
        typer.echo("")
        typer.echo("--- " + sheet_name + " (rows: " + str(len(rows)) + ") ---")
        for row in rows[:10]:
            typer.echo(" | ".join(row[:5]))


@office_app.command("read-pptx")
def office_read_pptx(path: str = typer.Argument(..., help="Path to .pptx file")):
    """Read a .pptx file."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.tools.office import read_pptx as _read_pptx
    result = _read_pptx(path)
    if "error" in result:
        typer.echo("Error: " + result["error"])
        raise SystemExit(1)
    typer.echo("Slides: " + str(result['metadata']['slide_count']))
    for slide in result['slides'][:5]:
        typer.echo("")
        typer.echo("Slide " + str(slide['index']) + ": " + slide['title'])
        for text in slide['texts'][:3]:
            typer.echo("  - " + text[:80])


app.add_typer(office_app, name="office")


browser_app = typer.Typer(help="Browser automation tools (AXTree, screenshots)")


@browser_app.command("axtree")
def browser_axtree(url: str = typer.Argument(..., help="URL to extract AXTree from"),
                   mode: str = typer.Option("headless", "--mode", help="headless or headed")):
    """Get the accessibility tree from a URL."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.tools.browser import get_axtree
    import asyncio
    result = asyncio.run(get_axtree(url, mode=mode))
    if "error" in result:
        typer.echo("Error: " + result["error"])
        raise SystemExit(1)
    typer.echo("URL: " + result['url'])
    typer.echo("Title: " + result['title'])
    typer.echo("Elements: " + str(result['element_count']))
    typer.echo("---")
    typer.echo(result['snapshot'][:2000])


@browser_app.command("text")
def browser_text(url: str = typer.Argument(..., help="URL to extract text from"),
                 mode: str = typer.Option("headless", "--mode", help="headless or headed")):
    """Extract readable text from a URL."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.tools.browser import extract_text
    import asyncio
    result = asyncio.run(extract_text(url, mode=mode))
    if "error" in result:
        typer.echo("Error: " + result["error"])
        raise SystemExit(1)
    typer.echo("URL: " + result['url'])
    typer.echo("Title: " + result['title'])
    typer.echo("---")
    typer.echo(result.get('text', '')[:2000])


@browser_app.command("screenshot")
def browser_screenshot(url: str = typer.Argument(..., help="URL to screenshot"),
                       mode: str = typer.Option("headless", "--mode", help="headless or headed"),
                       full: bool = typer.Option(False, "--full", help="Full page screenshot")):
    """Take a screenshot of a URL."""
    import sys
    sys.path.insert(0, str(_get_project_root()))
    from core.tools.browser import screenshot as _screenshot
    import asyncio
    result = asyncio.run(_screenshot(url, mode=mode, full_page=full))
    if "error" in result:
        typer.echo("Error: " + result["error"])
        raise SystemExit(1)
    typer.echo("Screenshot saved to: " + result['screenshot_path'])


app.add_typer(browser_app, name="browser")
app.add_typer(agent_app, name="agents")


if __name__ == "__main__":
    app()
