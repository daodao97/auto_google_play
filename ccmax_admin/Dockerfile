FROM node:22-alpine AS web-builder
WORKDIR /build/web
RUN corepack enable && corepack prepare pnpm@10.20.0 --activate
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM golang:1.25-alpine AS builder
WORKDIR /build
RUN apk add --no-cache git
COPY go.mod go.sum ./
RUN go mod download
COPY . .
COPY --from=web-builder /build/web/dist ./web/dist
RUN CGO_ENABLED=0 go build -buildvcs=true -ldflags="-s -w" -trimpath -o app ./cmd

FROM alpine:3.22 AS final
WORKDIR /app
RUN apk add --no-cache tzdata && mkdir -p /app/data
COPY --from=builder /build/app /app/app
ENV TZ=Asia/Shanghai DATABASE_PATH=/app/data/ccmax.db BIND=:8080 COOKIE_SECURE=false
VOLUME ["/app/data"]
EXPOSE 8080
ENTRYPOINT ["/app/app"]
