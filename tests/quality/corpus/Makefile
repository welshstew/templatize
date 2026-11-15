APP      := billing-worker
VERSION  ?= 1.12.0
REGISTRY := ghcr.io/acme
PORT     = 9090

build:
	docker build -t $(REGISTRY)/$(APP):$(VERSION) .
