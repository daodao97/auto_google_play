.PHONY: install clean web web_dev run dev build typecheck test

APP_NAME = server
BUILD_DIR = $(PWD)/build

install:
	pnpm --dir web install --frozen-lockfile

clean:
	rm -rf ./build ./web/dist

web:
	pnpm --dir web run build

web_dev:
	pnpm --dir web run dev

run: web
	go run ./cmd --bind :4001

dev: web
	APP_ENV=dev go run ./cmd --bind :4001

build: web
	mkdir -p $(BUILD_DIR)
	go build -ldflags="-w -s" -o $(BUILD_DIR)/$(APP_NAME) ./cmd

typecheck:
	pnpm --dir web run typecheck

test: web
	go test ./... -timeout 60s
