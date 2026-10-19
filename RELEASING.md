# Releasing attache

A release is a pushed tag. The tag check, the full CI matrix, the build, the PyPI upload and the
GitHub release all happen in `.github/workflows/release.yml`.

## One-time setup: PyPI Trusted Publishing

attache uploads to PyPI over OIDC. There is no API token in this repository and there should
never be one. PyPI needs to be told, once, which workflow it accepts uploads from.

`attache` has not been published yet, so this is a **pending publisher**. It becomes an ordinary
publisher the moment the first release lands, and that release also claims the name.

1. Sign in at <https://pypi.org> and open **Account settings → Publishing**.
2. Under *Add a new pending publisher*, choose **GitHub** and fill in exactly:

   | Field | Value |
   |---|---|
   | PyPI project name | `attache` |
   | Owner | `OpenAfterHours` |
   | Repository name | `attache` |
   | Workflow name | `release.yml` |
   | Environment name | *(leave blank while the repo is private)* |

3. Save. The fields must match the workflow exactly or the upload is rejected.

### Later: the `pypi` environment

GitHub's free plan only allows environments in public repositories, so the publish job runs
without one for now. When the repository is made public:

1. In `release.yml`, add `environment: {name: pypi, url: https://pypi.org/p/attache}` to the
   `publish` job.
2. On PyPI, edit the publisher and set **Environment name** to `pypi`.
3. Optionally add a required reviewer under **Settings → Environments → pypi** so a person
   approves each upload.
4. Add a tag ruleset (**Settings → Rules → Rulesets**) so only maintainers can create `v*`
   tags. Until then, anyone with write access can publish by pushing a tag.

## Cutting a release

Versions follow PEP 440. The single source of truth is `__version__` in
`src/attache/__init__.py`; `pyproject.toml` reads it.

1. Bump `__version__` in a pull request and merge it once CI is green.
2. Tag the merge commit on `main` and push the tag:

   ```bash
   git checkout main
   git pull
   git tag v0.0.1
   git push origin v0.0.1
   ```

3. Watch **Actions → Release**. In order, it:
   - checks the tag matches `__version__`;
   - runs the whole CI workflow (lint, tests on Ubuntu, Windows and macOS, the wheel smoke test);
   - builds the sdist and wheel and checks their metadata with `twine check --strict`;
   - uploads them to PyPI;
   - creates the GitHub release with generated notes and both files attached.

A tag that doesn't match `__version__` fails in the first job, before anything is built. Versions
ending in `a`, `b`, `rc` or `dev` (for example `0.1.0rc1`) are marked as pre-releases on GitHub.

## If a release fails

- **Before the publish job:** fix the problem on `main`, delete the tag
  (`git push --delete origin v0.0.1` and `git tag -d v0.0.1`), and tag again.
- **After the upload:** PyPI never accepts the same version twice. Bump to the next version and
  release that.
