---
icon: lucide/package
---

# Releasing

A release is a pushed tag. The tag check, the full CI matrix, the build, the PyPI upload and
the GitHub release all happen in
[`.github/workflows/release.yml`](https://github.com/OpenAfterHours/attache/blob/main/.github/workflows/release.yml).

## One-time setup: PyPI Trusted Publishing

Attaché uploads to PyPI over OIDC. There is no API token in the repository, and there should
never be one. PyPI needs to be told, once, which workflow it accepts uploads from.

`attache` has not been published yet, so this is a **pending publisher**. It becomes an
ordinary publisher the moment the first release lands, and that release also claims the
name.

1. Sign in at <https://pypi.org> and open **Account settings → Publishing**.
2. Under *Add a new pending publisher*, choose **GitHub** and fill in exactly:

    | Field | Value |
    |---|---|
    | PyPI project name | `attache` |
    | Owner | `OpenAfterHours` |
    | Repository name | `attache` |
    | Workflow name | `release.yml` |
    | Environment name | `pypi` |

3. Save. All five fields must match, or the upload is rejected. The environment name in
   particular is easy to leave blank, and it is part of the claim.

### The `pypi` environment

The `publish` job runs in a GitHub environment named `pypi`; GitHub creates it on the first
release. It pins the Trusted Publishing claim, and it is where an approval gate goes:

- **Settings → Environments → pypi → Required reviewers** makes a person approve each
  upload. Leave it unprotected and a pushed tag publishes on its own, which is the intended
  default.
- A tag ruleset (**Settings → Rules → Rulesets**, target `v*`) limits who can create release
  tags. Without one, anyone with write access can publish by pushing a tag.

## Cutting a release

Versions follow PEP 440. The single source of truth is `__version__` in
`src/attache/__init__.py`; `pyproject.toml` reads it.

1. Bump `__version__` in a pull request, and merge it once CI is green.
2. Tag the merge commit on `main` and push the tag:

    ```bash
    git checkout main
    git pull
    git tag v0.0.1
    git push origin v0.0.1
    ```

3. Watch **Actions → Release**. In order, it:
    - checks the tag matches `__version__`;
    - runs the whole CI workflow (lint, tests on Ubuntu, Windows and macOS, the wheel smoke
      test);
    - builds the sdist and wheel, and checks their metadata with `twine check --strict`;
    - uploads them to PyPI;
    - creates the GitHub release, with generated notes and both files attached. If a release
      for the tag already exists (made by hand, say), it keeps that release's notes and
      attaches the files to it instead.

A tag that doesn't match `__version__` fails in the first job, before anything is built.
Versions ending in `a`, `b`, `rc` or `dev` (for example `0.1.0rc1`) are marked as
pre-releases on GitHub.

## If a release fails

- **Before the publish job:** fix the problem on `main`, delete the tag
  (`git push --delete origin v0.0.1` and `git tag -d v0.0.1`), and tag again.
- **After the upload:** PyPI never accepts the same version twice. Bump to the next version
  and release that.

## The documentation site

The docs publish themselves: `.github/workflows/docs.yml` builds the site on every pull
request that touches it, and deploys it to GitHub Pages on every such push to `main`. It
needs one setting, once: **Settings → Pages → Build and deployment → Source** set to
**GitHub Actions**.

This page is adapted from
[`RELEASING.md`](https://github.com/OpenAfterHours/attache/blob/main/RELEASING.md); change
both together.
