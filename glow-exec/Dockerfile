# Builds the glow-exec step runtime as one static binary in an otherwise
# empty image. An init container copies /glow-exec into each step pod.
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /out/glow-exec ./cmd/glow-exec

FROM scratch
COPY --from=build /out/glow-exec /glow-exec
ENTRYPOINT ["/glow-exec"]
