# AGENTS.md — Analysis Tool For HyperOS

Python CLI that downloads HyperOS ROMs, extracts images and version-tracks APKs into JSON sets, released with a **self-built pipeline**: push to `main` → beta (VERSION file X.Y.Z + server BUILD → four-segment `X.Y.Z.N`, no GitHub Release); human tag `X.Y.Z.0` → stable (GitHub Release latest). Two channels only (beta / stable; no alpha). No marker commits, no hand-written release notes.

## Commands

| Purpose | Command |
| --- | --- |
| Install (only Python dep) | `pip install -r requirements.txt` (`pyaxmlparser==0.3.31`) |
| Usage / all flags | `python3 main.py -h` (flow `-d` → `-p` → `-i` → `-f` → …) |
| Version dry-run (no publish) | `python3 ci/release.py version --channel beta`（读服务器 BUILD；打印 version_name/code） |
| Changelog dry-run (no publish) | `python3 ci/release.py changelog --out /tmp/changelog.txt`（全量历史） |
| Script checks | `python3 -m py_compile ci/release.py ci/test_release.py && python3 ci/test_release.py` |
| Local commit gate | `git config core.hooksPath .githooks`（一次性；`.githooks/commit-msg` 调 `release.py check-msg`） |
| Workflow YAML parse | `python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" .github/workflows/*.yml` |
| No server paths in source | `grep -rn '/www/' ci .github && echo FAIL \|\| echo OK` |

No product test suite; release.py 的纯函数有 unittest 覆盖，其余是本地手检。 Local run needs Python 3.x + `aria2c` + 7zip; distributed zips bundle `payload-dumper-go` and `extract.erofs` in `tools/` (CI fetches them via `gh release download` — upstream 500s → rerun). The self-hosted runner's workspace persists, so check stale `output/` before trusting artifacts.

## Release model (self-built, two channels)

Push `main` → beta `X.Y.Z.N` (BUILD 1–99, signed `version.json`, no GitHub Release); human-pushed tag `X.Y.Z.0` → stable (GitHub Release latest). CI chain `python3 ci/release.py version` → `changelog` → `pack` → `upload` computes the version, the incremental changelog (full history on first release), the 8-platform pack and the upload; the tag only triggers the stable build, never version or changelog. Rules: `~/.config/opencode/docs/versioning.md`; implementation: `release-pipeline.md`.

## Context loading

| Task | Read |
| --- | --- |
| Release rules / templates | `~/.config/opencode/docs/versioning.md`, `release-pipeline.md`, `committing.md` |
| Shared across the three Analysis tools | `~/.config/opencode/docs/sister-repos.md` |
| CI / deploy behavior | `.github/workflows/release.yml`, `ci/release.py` |
| End-user usage | `README.md` |
| Global rules | `~/.config/opencode/AGENTS.md` |

## Boundaries

### Always
- Use the `docs:` prefix for doc-only commits (`paths-ignore` skips the release; hidden changelog types: `committing.md` §Types and changelog sections).
- Keep `ci/release.py`'s `PLATFORMS` table in sync with upstream asset names; `gh release download` patterns were verified against live assets.
- Keep `ci/release.py` free of hard-coded server paths (`OPENLIST_STORAGE_DIR` comes from secret `SERVER_OPENLIST_DIR`).

### Ask first
- Changing the platform table, artifact naming, OpenList retention, or `.github/workflows/release.yml`.
- Bumping a dependency pin in `ci/release.py` (erofs macOS tag is pinned because newer builds dropped `extract.erofs`).
- Beta/Stable tags.

### Never
- Hand-edit `CHANGELOG.md`, `changelog.txt` or `version.json` on the server — CI regenerates and uploads them.
- Commit build output (`output/`, `*.zip`, `__pycache__/`, `venv/`) or extracted ROM dirs.
- Reintroduce semantic-release, `release.config.mjs`, marker commits, `release-notes.md`, or `ci/release-*.sh` scripts.
- Treat the two channels as branches — there is no `beta` branch; channels are version states.

## Definition of done

`python3 ci/test_release.py` 通过、`python3 -m py_compile ci/release.py` 无错，every `.github/workflows/*.yml` parses；`release.py version` / `changelog` dry-runs print sane output；no `/www/` server paths in source；nothing from `output/`, `*.zip` or `__pycache__/` staged。
