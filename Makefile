CLUSTER ?= infra-healer
NS      ?= demo

.PHONY: preflight cluster build deploy demo-up crash leak watch test clean
preflight:
	@for t in docker kind kubectl; do command -v $$t >/dev/null || { echo "missing '$$t' - install it first (e.g. brew install $$t)"; exit 1; }; done
	@docker info >/dev/null 2>&1 || { echo "Docker daemon is not running"; exit 1; }
cluster: preflight  ## create kind cluster
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --name $(CLUSTER)
build:
	docker build -t demo-service:dev demo-service
	kind load docker-image demo-service:dev --name $(CLUSTER)
deploy:
	kubectl apply -f k8s/demo-service.yaml
	kubectl -n $(NS) rollout status deploy/demo-service --timeout=90s
demo-up: cluster build deploy
crash:    ## persistent crash -> CrashLoopBackOff
	kubectl -n $(NS) exec deploy/demo-service -- python -c "import urllib.request as u; u.urlopen(u.Request('http://localhost:8080/crash?persist=true', method='POST'))"
leak:     ## ~10MB per call until OOMKilled (64Mi limit)
	for i in 1 2 3 4 5 6 7; do kubectl -n $(NS) exec deploy/demo-service -- python -c "import urllib.request as u; print(u.urlopen(u.Request('http://localhost:8080/leak?mb=10', method='POST')).read())" || break; done
watch:
	kubectl -n $(NS) get pods -w
test:
	cd demo-service && python3 -m pytest -q
clean:
	kind delete cluster --name $(CLUSTER)
