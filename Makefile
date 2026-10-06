CLUSTER ?= infra-healer
NS      ?= demo

.PHONY: preflight cluster build deploy demo-up crash leak metrics leak-slow watch agent-watch agent-run incidents test clean
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
metrics:  ## install metrics-server in kind (needed for predictive rules)
	kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
	kubectl -n kube-system patch deploy metrics-server --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
	kubectl -n kube-system rollout status deploy/metrics-server --timeout=120s
leak-slow:  ## ~1MB every 5s: a gradual leak the predictive rules should catch before OOM
	for i in $$(seq 1 60); do kubectl -n $(NS) exec deploy/demo-service -- python -c "import urllib.request as u; u.urlopen(u.Request('http://localhost:8080/leak?mb=1', method='POST'))" || break; sleep 4; done
leak:     ## ~10MB per call until OOMKilled (64Mi limit)
	for i in 1 2 3 4 5 6 7; do kubectl -n $(NS) exec deploy/demo-service -- python -c "import urllib.request as u; print(u.urlopen(u.Request('http://localhost:8080/leak?mb=10', method='POST')).read())" || break; done
watch:
	kubectl -n $(NS) get pods -w
test:
	cd demo-service && python3 -m pytest -q
	python3 -m pytest -q
agent-watch:  ## run the watcher against the demo namespace
	python3 -m agent.monitor.watcher --namespace $(NS) --interval 2
agent-run:  ## run the full healing loop (MODE=OBSERVE_ONLY|HUMAN_APPROVAL|AUTONOMOUS)
	python3 -m agent.main run --mode $(or $(MODE),HUMAN_APPROVAL)
incidents:
	python3 -m agent.main list
clean:
	kind delete cluster --name $(CLUSTER)
