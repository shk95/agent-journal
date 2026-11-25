# Agent Journal repository instructions

- Keep server, client, web, and tests in this repository. Run the checks in `docs/SETUP.md` before committing.
- Use English by default for product UI, generated summaries, diagnostics, source comments, repository documentation, and Git messages. Preserve the original language of transcripts and user-authored notes.
- Use `master` as the default branch.
- Treat every tracked file, commit message, commit identity, issue, and PR as public. Use a GitHub noreply address for both author and committer.
- Create commits with `TZ=UTC git commit` so timestamps use UTC.
- Use generated or hand-written synthetic fixture content. Keep real transcripts, prompts, notes, Git metadata exports, device identifiers, hostnames, addresses, and credentials out of the repository and public discussion.
- Keep runtime databases, credentials, machine configuration, captures, backups, and logs outside the tracked tree. Use environment variables or an ignored local directory. Examples must be portable and use relative paths, loopback addresses, or reserved example domains.
- Before publishing, explicitly stage reviewed files, inspect the staged diff, then run `python3 scripts/check_public.py` and `gitleaks git --staged --redact --ignore-gitleaks-allow`. Run `python3 scripts/check_public.py --history` and `gitleaks git --redact --ignore-gitleaks-allow --log-opts="master"` after committing and before pushing.
- Review the diff for private names and contextual disclosures that automated checks cannot infer. If material is staged by mistake, remove it from the index. If a secret enters commit history, rotate it and stop the push until the history has been cleaned and checked again.
