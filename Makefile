.PHONY: test test-api test-extension test-mobile compose-check

test: test-api test-extension test-mobile compose-check

test-api:
	.venv/bin/python -B scripts/test_api.py

test-extension:
	cd apps/extension && npm test && npm run check

test-mobile:
	cd apps/mobile && npm run typecheck

compose-check:
	docker compose -f infra/compose.yaml config --quiet

