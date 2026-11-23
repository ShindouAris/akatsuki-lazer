FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/multiplayer ./cmd/multiplayer

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/multiplayer /multiplayer
EXPOSE 8081
ENTRYPOINT ["/multiplayer"]
