# syntax=docker/dockerfile:1

# The build context is status/. catalog.yaml lives at the repository root, outside it, and is
# mounted at run time (/app/catalog.yaml) rather than baked in: the catalog changes more often than
# this service, and an image that carried a stale copy would quietly disagree with Jenkins.

FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /source

# Restore on its own layer, keyed only by the files that decide it, so a source-only change reuses it.
COPY Directory.Build.props Directory.Packages.props ./
COPY src/Yggdrasil.Status/Yggdrasil.Status.csproj src/Yggdrasil.Status/
RUN dotnet restore src/Yggdrasil.Status/Yggdrasil.Status.csproj

COPY src/ src/
RUN dotnet publish src/Yggdrasil.Status/Yggdrasil.Status.csproj \
        --configuration Release --no-restore --output /app

# Chiseled: no shell, no package manager, non-root (the image's "app" user, UID 1654) by default.
# The service holds a token and sits on the edge network, so the less there is in the image the
# better. The health check needs no curl: the service checks itself (--healthcheck).
FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble-chiseled AS final
WORKDIR /app
COPY --from=build /app .

# Kestrel's ports are set in code (8080 public, YGGDRASIL_STATUS_INTERNAL_PORT internal); clearing
# the image's default keeps Kestrel from warning that it overrides it.
ENV ASPNETCORE_HTTP_PORTS= \
    YGGDRASIL_CATALOG_PATH=/app/catalog.yaml

# 8080: /api and /healthz, routed by Traefik. 8081: /internal, for Prometheus over the telemetry
# network; never published, never routed. Moving the internal port means moving this too.
EXPOSE 8080 8081

USER $APP_UID

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["dotnet", "Yggdrasil.Status.dll", "--healthcheck"]

ENTRYPOINT ["dotnet", "Yggdrasil.Status.dll"]
