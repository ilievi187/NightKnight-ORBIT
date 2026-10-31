# ORBIT / LUNA Mission — Private Skill Vault & Fleet Sync

## Mission

Inventory all agent skills across the **Ubuntu LUNA server**, **NightKnightPC (Omarchy)**, and **detti** (Windows 11 where available). Consolidate unique skills without destroying per-agent variants or leaking private content into public GitHub. Research relevant new skills and stage candidates for review, not for blind automatic execution.

The source code of `tools/skill_sync.py` is public; **skill contents, inventories and sensitive absolute paths must remain private.** The vault is a separate local-only directory (default `~/.local/share/orbit-skill-vault`). Do not `git add` this vault or publish it in this repository.

## Supported flow (Python 3.11+; only standard library)

```bash
# On Ubuntu / NightKnightPC: discover available SKILL.md trees; read-only.
python3 tools/skill_sync.py inventory > /tmp/orbit-skills-local.json
# Include additional LUNA-specific folders discovered on disk:
python3 tools/skill_sync.py inventory --source ~/.hermes/skills --source /actual/luna/skills

# Stage ONE selected skill (read-only unless --apply is supplied):
python3 tools/skill_sync.py stage /absolute/source/skill --apply
# Returns a SHA256 digest; inspect SKILL.md and all files, dependencies, references and warnings.
# Exact digest approval; never skip review:
python3 tools/skill_sync.py approve skill-name --sha256 <exact-digest> --apply

# Preview and then deploy approved skills into shared directory:
python3 tools/skill_sync.py deploy --target ~/.agents/skills
python3 tools/skill_sync.py deploy --target ~/.agents/skills --apply
```

**Rules:** Never run installer scripts from unreviewed skills. No automatic `curl | bash`, `npx` or `pip install`; don't auto-approve downloaded content. `deploy` never overwrites a local skill — conflict must be reviewed manually. `stage` and `approve` copy and verify file SHA256, rejecting symlinks/credential file names; advisory warnings are **not** a security guarantee. Human review of source and license is mandatory. Do not treat 120 vs 58 counts as verified until real inventory is collected.

## Hermes integration (non-destructive)

Official docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/

Add to `~/.hermes/config.yaml` after reviewing existing configuration (merge; don't replace entire config):

```yaml
skills:
  external_dirs:
    - ~/.agents/skills
```

**Security:** `external_dirs` is not a read-only barrier. Hermes can edit these files if its Unix user has write permission. A separate read-only shared mount or filesystem ACL is recommended for multi-agent sharing. Project-specific and local skills shadow same-name external entries. Validate by running `hermes skills list` in a new session, but do not assume `hermes` or the exact profile CLI is installed on every machine.

## Cross-machine transport plan

Do not sync any vault data to public GitHub. First verify authorized **Tailscale/SSH names, identities, host keys, and per-machine source directories**. From Ubuntu, an explicit operator-approved example for the approved subtree only:

```bash
# Preview changes before transfer (replace host and actual user paths):
rsync -avnc --ignore-existing -e ssh ~/.local/share/orbit-skill-vault/approved/ USER@NIGHTKNIGHTPC:~/.local/share/orbit-skill-vault/approved/
# After reviewing the dry-run output, remove n to transfer files.
# Then run `deploy --apply` locally on NightKnightPC.
```

The `rsync` example is a **plan, not an executed action**. With `--ignore-existing` it does not delete or overwrite destination files; use dry-run and compare hashes before a real transfer. Divergent skills need manual conflict resolution. For Windows `detti`, verify whether OpenSSH server and the desired Windows agent skill directory exist before choosing SFTP/rsync; don't invent device support. Never copy an SSH private key, personal token, private prompts, or environment file to the public repository.

## External skill source shortlist (research before adopting)

- **NousResearch/Hermes official hub:** https://hermes-agent.nousresearch.com/docs/guides/work-with-skills/ — first choice for Hermes-compatible optional skills.
- **Anthropic:** https://github.com/anthropics/skills — patterns for frontend design, webapp testing, document workflows, with per-skill licenses.
- **OpenAI/Codex:** https://github.com/openai/skills — curated and experimental reusable workflows, with per-skill license.
- **Vercel/community index:** https://skills.sh — discovery signal, not a trust rating or blanket installation authority.
- **Open standard:** https://agentskills.io/specification — skill format and frontmatter validation.

Prioritize **GitHub PR workflows, debugging/testing, browser testing, frontend design, docs, security auditing and system administration** useful for ORBIT/AURORA/NightKnight projects. Avoid social posting, accounts, crypto, paid API setups and opaque third-party scripts unless explicitly needed.

## Requested LUNA outputs

1. Inventory table per machine: skill name, source path, SHA256, duplicates/conflicts, validity, script warnings.
2. Private vault staging list and reviewed exact digests; which were approved by the operator.
3. Evidence of deployed skills and `hermes skills list` verification on each available host.
4. Source catalog with license, upstream commit/version and fit; **no blind mass-install**.
5. Automated tests and CI for the synchronizer; PR in ORBIT for code only, no private inventories.
6. Existing workloads and Heretic sessions untouched; report offline machine/auth blockers rather than retrying passwords.

A skill repository is not proof its code is safe. The initial tool performs basic checks only; a thorough third-party review should cover supply-chain, script behavior and prompt-injection instructions before activation.
