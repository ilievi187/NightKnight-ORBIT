# Prompt: Ubuntu LUNA — skill inventory + safe sharing

You are LUNA on the Ubuntu server. Implement ORBIT Issue #5: private agent-skill inventory and sharing. Repo: https://github.com/ilievi187/NightKnight-ORBIT ; refer to docs/agents/LUNA-SKILLS-FLEET.md. Do not assume repo is already cloned, or that your agent has any tools not currently exposed.

Proceed in small validated steps:
1. Determine the actual host, repo, and available shell/Git access; read AGENTS.md and the fleet guide. Preserve existing processes and changes; don't modify another project.
2. Run the read-only inventory. Locate all skill roots actually in use by LUNA and Hermes, then (via already-authorized SSH/Tailscale only) NightKnightPC and detti. Log names, versions, hashes, duplicates and missing hosts **privately**, never in public commits.
3. Audit duplicate names and content. Build a private approved-vault plan. Stage candidates only; approvals require explicit human review of exact hashes. No global auto-install of downloaded skills.
4. Research NousResearch official skills, Anthropic, OpenAI, and skills.sh. Identify a small high-value shortlist, include licensing, versions/commits, and code/security review.
5. Verify `tools/skill_sync.py` tests and extend tests for a real problem found. Create a feature branch, push a PR against ORBIT only. Do not auto-merge, expose credentials, or redeploy models.
6. Summarize actual executed commands, reachable machines, tested results, rejected content, new approved skills, PR URL, and any blocker.

You may use the network for documented research and verified approved SSH peers, but never execute third-party skill scripts automatically or copy private skills into a public repo. Don't alter live Heretic GPU sessions.
