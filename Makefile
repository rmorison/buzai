# buzai — setup & operate front door.
#
# This is a TASK RUNNER, not a build system: the targets are .PHONY verbs that wrap
# the helper scripts and lifecycle commands documented in docs/SETUP.md. `make help`
# is the index. The verifiable steps (venv, trust-install, trust-check, service-*) are
# deterministic; the irreducibly-interactive ones (auth, prime-consent) drop you into
# the real command — the docs carry the answers to give.
#
# All paths are relative to the repo root (trust/, scripts/, deploy/), so run `make`
# from the top of your clone.
#
# Verb NAMES are public API — keep them stable. Recipes may evolve.

NAME    ?= buzai-assistant
WORKDIR ?= $(HOME)/buzai
PY      := .venv/bin/python
SYSTEMD_USER := $(HOME)/.config/systemd/user
UNIT    := $(SYSTEMD_USER)/claude-remote.service

.DEFAULT_GOAL := help

# `audit` MUST be .PHONY — a live workspace has an `audit/` directory, and without this
# Make would treat the target as a satisfied file and skip the recipe.
.PHONY: help bootstrap setup claude-install venv test trust-install trust-check hub-init \
        liveness audit doctor smoke env-url auth prime-consent service-install service-start \
        service-stop service-restart service-status lint dev

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- build / setup ------------------------------------------------------------

bootstrap: ## Host bootstrap (root phase of install.sh): create service user, linger, XDG wiring — needs sudo
	sudo $(if $(BUZAI_COPY_SSH_KEYS),BUZAI_COPY_SSH_KEYS=$(BUZAI_COPY_SSH_KEYS)) BUZAI_USER=$(or $(BUZAI_USER),buzai) bash install.sh

setup: ## Drive the whole setup; probes skip done steps, pauses only at the 3 manual ones (re-run to resume)
	bash install.sh --setup

claude-install: ## Install Claude Code (official installer) if not already present
	@command -v claude >/dev/null 2>&1 && claude --version \
	  || { curl -fsSL https://claude.ai/install.sh | bash; echo "now: source ~/.profile  (or re-login) so PATH picks it up"; }

venv: ## Create the pinned .venv (uv) — zero deps, just the interpreter
	{ command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; } && \
	  export PATH="$$HOME/.local/bin:$$PATH" && \
	  uv venv --python $$(cat .python-version) .venv
	@echo "venv ready -> $(PY) $$($(PY) --version)"

test: ## Run the full test suite (trust gate + scripts)
	$(PY) -m unittest discover -t . -s trust/tests -p 'test_*.py'
	$(PY) -m unittest discover -t . -s scripts/tests -p 'test_*.py'

trust-install: ## Merge the trust-gate hooks into .claude/settings.json + lock audit/ (idempotent)
	$(PY) scripts/install_hooks.py
	mkdir -p audit && chmod 700 audit
	@echo "trust gate wired; RESTART your claude session so the hooks load (settings are snapshotted at session start)"

trust-check: ## Validate trust-gate config parses + the gate self-test passes
	$(PY) scripts/trust_check.py

hub-init: ## Create + seed the private hub repo outside the checkout (idempotent; stops before the remote)
	$(PY) scripts/hub_init.py

# --- service ------------------------------------------------------------------

service-install: ## Install the --user systemd unit (override NAME=/WORKDIR= if not ~/buzai)
	@test -n "$(strip $(NAME))" || { echo "error: NAME must not be empty"; exit 1; }
	mkdir -p $(SYSTEMD_USER)
	cp deploy/claude-remote.service.template $(UNIT)
# $(subst &,\&,…) escapes sed's '&' (whole-match) metachar; '|' delimiter dodges the '#'
# in any %h path. NAME with '/' or WORKDIR with '|' are still unsupported (pathological).
ifneq ($(NAME),buzai-assistant)
	sed -i 's/--name buzai-assistant/--name $(subst &,\&,$(NAME))/' $(UNIT)
endif
ifneq ($(WORKDIR),$(HOME)/buzai)
	sed -i 's|%h/buzai|$(subst &,\&,$(WORKDIR))|g' $(UNIT)
endif
	systemctl --user daemon-reload
	@echo "installed $(UNIT)"
	@echo "Remote Control needs a one-time 'make prime-consent' (the #1 trap) done BEFORE"
	@echo "'make service-start' — do it now if you haven't. Then: make service-start  +  loginctl enable-linger $$USER"

service-start: ## Enable + start the service now and on boot
	systemctl --user enable --now claude-remote

service-stop: ## Stop the service
	systemctl --user stop claude-remote

service-restart: ## Restart the service (the standard bounce)
	systemctl --user restart claude-remote

service-status: ## Show service status
	systemctl --user status claude-remote --no-pager

# --- health / operate ---------------------------------------------------------

liveness: ## LIVE / NOT-LIVE — is the remote-control server holding a relay socket?
	$(PY) scripts/liveness.py

audit: ## Show the last trust-gate decisions (verify the gate is firing; pass N=… to change count)
	$(PY) scripts/audit_tail.py -n $(or $(N),15)

doctor: ## Aggregate health check: prereqs, venv, secrets, liveness (informational)
	@echo "== prereqs =="; command -v claude >/dev/null && claude --version || echo "  claude: MISSING (see SETUP step 2)"
	@echo "== venv ==";    test -x $(PY) && echo "  $$($(PY) --version)" || echo "  .venv: MISSING (run: make venv)"
	@echo "== secrets =="; if $(PY) scripts/secrets_preflight.py; then :; else echo "  secrets-preflight reported a problem (exit $$?)"; fi
	@echo "== liveness =="; if $(PY) scripts/liveness.py; then :; else echo "  not live (exit $$?) — a traceback above means the probe itself errored, not just idle"; fi

smoke: ## SC0 preflight for the 8-point smoke test (see docs/SMOKE-TEST.md)
	$(PY) -m scripts.smoke_test

env-url: ## Print the owner-equivalent reconnect deep-link (TTY only — refuses to be redirected)
	$(PY) scripts/env_url.py

# --- interactive (make hosts these; the docs carry the answers to give) -------

auth: ## Interactive Claude.ai subscription login — pick "1. Claude account with subscription"
	claude

prime-consent: ## One-time Remote Control consent — answer "y", then spawn mode "1"
	claude remote-control --name $(NAME)

# --- dev ----------------------------------------------------------------------

lint: ## [dev] ruff-lint the python sources
	ruff check trust scripts

dev: ## [dev] Install the pre-commit hooks (hygiene, ruff, secret scan, leak gate)
	{ command -v pre-commit >/dev/null || { command -v uv >/dev/null && uv tool install pre-commit; } || pipx install pre-commit; }
	pre-commit install
