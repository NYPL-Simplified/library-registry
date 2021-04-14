.PHONY: help build db-session webapp-shell up up-watch start stop down clean full-clean
.DEFAULT_GOAL := help

help:
	@echo "Usage: make [COMMAND]"
	@echo ""
	@echo "Commands:"
	@echo ""
	@echo "    build          - Build the libreg_webapp and libreg_local_db images"
	@echo "    db-session     - Start a psql session as the superuser on the db container"
	@echo "    webapp-shell   - Open a shell on the webapp container"
	@echo "    up             - Bring up the local cluster in detached mode"
	@echo "    up-watch       - Bring up the local cluster, remains attached"
	@echo "    start          - Start a stopped cluster"
	@echo "    stop           - Stop the cluster without removing containers"
	@echo "    down           - Take down the local cluster"
	@echo "    clean          - Take down the local cluster and removes the db volume"
	@echo "    full-clean     - Take down the local cluster and remove containers, volumes, and images"

build:
	docker-compose build

db-session:
	docker exec -it libreg_local_db psql -U postgres

webapp-shell:
	docker exec -it libreg_webapp /bin/sh

up:
	docker-compose up -d

up-watch:
	docker-compose up

start:
	docker-compose start

stop:
	docker-compose stop

down:
	docker-compose down

clean:
	docker-compose down --volumes

full-clean:
	docker-compose down --volumes --rmi all