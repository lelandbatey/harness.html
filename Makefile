PY ?= python3
OUT := dist/harness.html

PUBLISH := dist/public/harness.html
FORCE_FLAG := $(if $(FORCE),--force,)

.PHONY: build fresh publish pages test clean

# Always rebuilds: a running server rewrites $(OUT) on every save, so its mtime says nothing about the
# sources. Data from a prior run in $(OUT) (sessions, settings, token) is carried into the new build;
# the previous file is kept as $(OUT).prev.
build:
	$(PY) tools/build.py -o $(OUT)

# An empty build (no data carried over). Refuses to replace a $(OUT) holding data unless FORCE=1.
fresh:
	$(PY) tools/build.py --fresh $(FORCE_FLAG) -o $(OUT)

# A clean copy to publish (e.g. as a download link): no sessions, settings, token or fingerprint. Opened from
# the web it shows the landing page. Refuses to replace a $(PUBLISH) holding data unless FORCE=1.
publish:
	$(PY) tools/build.py --fresh $(FORCE_FLAG) -o $(PUBLISH)

# Builds $(PUBLISH) and pushes it to the gh-pages branch (served by GitHub Pages at
# https://lelandbatey.github.io/harness.html/, with index.html redirecting to harness.html).
PAGES_DIR := dist/gh-pages
pages: publish
	rm -rf $(PAGES_DIR) && git worktree prune
	git fetch -q origin gh-pages
	git worktree add -f $(PAGES_DIR) origin/gh-pages --detach
	cp $(PUBLISH) $(PAGES_DIR)/harness.html
	cd $(PAGES_DIR) && git add -A && \
		(git diff --cached --quiet && echo "pages: no changes" || \
		 (git commit -q -m "Publish harness.html from $$(git -C $(CURDIR) rev-parse --short HEAD)" && git push origin HEAD:gh-pages))
	git worktree remove --force $(PAGES_DIR)

# Tests build their own empty copies; they never touch $(OUT). Runs everything in parallel (~10s): see
# test/run.py (-v, -j N, or patterns to pick jobs: `$(PY) test/run.py test_server browser`). The browser tests
# need node + chromium and are skipped without them.
test:
	$(PY) test/run.py

# Never deletes $(OUT): it holds the data of your sessions.
clean:
	rm -f $(OUT).tmp
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
