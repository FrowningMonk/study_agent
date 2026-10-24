# Project: study_agent

> **Personal Telegram bot + CLI that turns a firehose of tech articles into a curated, summarized, searchable knowledge base.**

---

## How This Project Works

**Project Context:** All project knowledge lives in `.claude/skills/project-knowledge/` — architecture, patterns, deployment. Read those files when you need specifics about the codebase, stack, data model, or operational setup.

**Default branch:** `main` (single-branch model — short-lived feature branches merged to `main` via PR; no `dev`).

**Library Documentation:** Always use Context7 MCP when you need code generation, setup/configuration steps, or library/API documentation. Resolve library IDs and fetch current docs automatically instead of relying on training data.

---

## Language

- Communicate with user, plans, plan-mode: Russian
- Tech docs (code, context, rules, CLAUDE.md, skills): English
- AI prompts: English
- User-facing docs (README, `docs/`): Russian

## Behavior

- No "Great question!", no filler, no water.
- NEVER use AskUserQuestion tool. Ask questions as plain text in chat instead.
- ALL deployments via GitHub CI/CD only — target state. During transition (before CI is set up), manual SSH deploy is acceptable only with explicit per-deploy confirmation from the owner.

## Task Planning

- Use TodoWrite for multi-step tasks (>1 step).
- When user asks for team/swarm of agents: use TeamCreate, not TaskCreate.

## Security

- NEVER ask user to write secrets in chat.
- Instead: provide instructions where to store them securely.
  - Local: `.env` files, config files.
  - CI/CD: GitHub Actions secrets.
- ALWAYS ask before deploy/push to main/production.
- ALWAYS ensure secrets are in `.gitignore`: `.env`, `*.key`, `credentials.json`, `secrets/`.
- Be cautious with external actions (push, deploy, send messages, create PRs). Ask before acting externally when uncertain.
