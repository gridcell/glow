# glow-exec

The GLOW step runtime: a static Go binary that stages a step's inputs, runs
its tool and uploads its outputs. See [docs/glow-exec.md](../docs/glow-exec.md)
for the parameters, phases and output format.

```bash
go vet ./...
go test ./...
CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o glow-exec ./cmd/glow-exec
docker build -t glow-exec .
```

| Package | Content |
| --- | --- |
| `cmd/glow-exec` | Command line: `run`, `stage`, `collect`, `eval`, `install` |
| `internal/params` | Decodes the step parameters from the environment and flags |
| `internal/manifest` | Selects the tool from a manifest and validates values with JSON Schema |
| `internal/cel` | CEL evaluation, custom functions and `${{ }}` substitution |
| `internal/stage`, `internal/run`, `internal/collect` | The three phases |
| `internal/prefix` | Local and S3 storage |
| `internal/mediatype` | Media type matching and extensions |
| `internal/jsonvalue`, `internal/workdir` | JSON decoding and the work directory layout |
