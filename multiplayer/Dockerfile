FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/multiplayer ./cmd/multiplayer \
 && CGO_ENABLED=0 go build -trimpath -o /out/spectator ./cmd/spectator \
 && CGO_ENABLED=0 go build -trimpath -o /out/metadata ./cmd/metadata

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/multiplayer /multiplayer
COPY --from=build /out/spectator /spectator
COPY --from=build /out/metadata /metadata
EXPOSE 8081 8082 8084
ENTRYPOINT ["/multiplayer"]
