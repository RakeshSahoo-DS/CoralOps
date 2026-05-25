"""
CoralOps — AI-Powered DevOps Command Center
Pirates of the Coral-bean Hackathon · Track 1: Enterprise Agent
Author: Rakesh Kumar Sahoo (The Lone Corsair)

Uses Coral to JOIN GitHub + Sentry + Slack in one query,
then feeds results to Claude AI for root cause analysis.
"""

import subprocess
import json
import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="CoralOps", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="../frontend"), name="static")

# ── CONFIG ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_USERNAME   = os.getenv("GITHUB_USERNAME", "RakeshSahoo-DS")


# ── CORAL HELPER ────────────────────────────────────────────────────────────
def coral(sql: str) -> list:
    """Run a Coral SQL query and return results as list of dicts."""
    try:
        result = subprocess.run(
            ["coral", "sql", "--format", "json", sql],
            capture_output=True, text=True, timeout=45
        )
        output = result.stdout.strip()
        if not output:
            return []
        # Coral may return JSONL (one object per line) or a JSON array
        try:
            parsed = json.loads(output)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            rows = []
            for line in output.splitlines():
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
            return rows
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Coral CLI not found. Run: coral --version")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── AI ANALYSIS ─────────────────────────────────────────────────────────────
async def analyze_with_claude(context: dict) -> str:
    """Send Coral query results to Claude for AI root cause analysis."""
    if not ANTHROPIC_API_KEY:
        return "⚠️ Set ANTHROPIC_API_KEY env variable to enable AI analysis."

    prompt = f"""You are CoralOps, an expert DevOps incident investigator.
You have queried GitHub, Sentry, and Slack simultaneously using Coral DB.
Here is the cross-source data:

GITHUB OPEN ISSUES ({len(context.get('github_issues', []))} found):
{json.dumps(context.get('github_issues', []), indent=2)}

SENTRY ERRORS ({len(context.get('sentry_issues', []))} found):
{json.dumps(context.get('sentry_issues', []), indent=2)}

SLACK CHANNELS ({len(context.get('slack_channels', []))} found):
{json.dumps(context.get('slack_channels', []), indent=2)}

GITHUB REPOS:
{json.dumps(context.get('repos', []), indent=2)}

Based on this cross-source data, provide:
1. **Incident Status** (Critical/Warning/Healthy)
2. **Root Cause Summary** (2-3 sentences)
3. **Top 3 Action Items** (what to fix first)
4. **Risk Assessment** (what could go wrong next)

Be concise, specific, and actionable. Use the actual data above."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        data = response.json()
        return data["content"][0]["text"]


# ── ROUTES ──────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("../frontend/index.html")


@app.get("/api/health")
def health():
    try:
        r = subprocess.run(["coral", "--version"], capture_output=True, text=True)
        return {"status": "ok", "coral": r.stdout.strip(), "ai": bool(ANTHROPIC_API_KEY)}
    except:
        return {"status": "error", "coral": "not found"}


@app.get("/api/repos")
def get_repos():
    data = coral(f"SELECT full_name, description, language, stargazers_count, updated_at FROM github.user_repos LIMIT 10")
    return {"repos": data, "count": len(data)}


@app.get("/api/sentry")
def get_sentry():
    issues = coral("SELECT id, title, level, status, first_seen, project FROM sentry.issues LIMIT 15")
    projects = coral("SELECT id, name, slug FROM sentry.projects LIMIT 10")
    return {"issues": issues, "projects": projects}


@app.get("/api/slack")
def get_slack():
    channels = coral("SELECT id, name, num_members FROM slack.channels LIMIT 10")
    return {"channels": channels}


@app.get("/api/dashboard")
async def dashboard():
    """
    Main CoralOps endpoint: queries all 3 sources and runs AI analysis.
    This is the core feature — Coral querying GitHub + Sentry + Slack
    and Claude analyzing the combined results.
    """
    # Query all 3 sources via Coral
    repos = coral(
        f"SELECT full_name, language, stargazers_count, updated_at "
        f"FROM github.user_repos LIMIT 6"
    )
    github_issues = coral(
        f"SELECT number, title, state, created_at "
        f"FROM github.issues "
        f"WHERE owner = '{GITHUB_USERNAME}' AND repo = 'hospital-patient-records' AND state = 'open' "
        f"LIMIT 10"
    )
    sentry_issues = coral(
        "SELECT id, title, level, status, first_seen, project "
        "FROM sentry.issues LIMIT 10"
    )
    slack_channels = coral(
        "SELECT id, name, num_members FROM slack.channels LIMIT 8"
    )

    # Build context for AI
    context = {
        "repos": repos,
        "github_issues": github_issues,
        "sentry_issues": sentry_issues,
        "slack_channels": slack_channels,
    }

    # AI analysis
    ai_report = await analyze_with_claude(context)

    return {
        "sources_queried": 3,
        "repos": repos,
        "github_issues": github_issues,
        "sentry_issues": sentry_issues,
        "slack_channels": slack_channels,
        "ai_report": ai_report,
        "summary": {
            "repo_count": len(repos),
            "open_issues": len(github_issues),
            "sentry_errors": len(sentry_issues),
            "slack_channels": len(slack_channels),
            "critical_errors": len([i for i in sentry_issues if i.get("level") in ["fatal", "error"]]),
        }
    }


class QueryBody(BaseModel):
    sql: str


@app.post("/api/query")
def custom_query(body: QueryBody):
    """Run any Coral SQL query."""
    data = coral(body.sql)
    return {"results": data, "count": len(data), "sql": body.sql}


@app.post("/api/query/analyze")
async def query_and_analyze(body: QueryBody):
    """Run a Coral SQL query AND get AI analysis of results."""
    data = coral(body.sql)
    context = {"query_results": data, "sql": body.sql}

    if not ANTHROPIC_API_KEY:
        return {"results": data, "analysis": "Set ANTHROPIC_API_KEY to enable AI analysis."}

    prompt = f"""You ran this Coral SQL query across multiple data sources:
SQL: {body.sql}

Results ({len(data)} rows):
{json.dumps(data[:20], indent=2)}

Provide a brief 3-sentence analysis of what this data reveals. Be specific and actionable."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        ai_data = response.json()
        analysis = ai_data["content"][0]["text"]

    return {"results": data, "count": len(data), "analysis": analysis}


if __name__ == "__main__":
    print("🪸 CoralOps starting...")
    print(f"   GitHub: {GITHUB_USERNAME}")
    print(f"   AI: {'enabled' if ANTHROPIC_API_KEY else 'disabled (set ANTHROPIC_API_KEY)'}")
    print("   Open: http://localhost:8000")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
