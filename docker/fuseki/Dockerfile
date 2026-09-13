FROM eclipse-temurin:21-jre-jammy

ARG JENA_VERSION=6.2.0
ARG JENA_DIST_BASE=https://archive.apache.org/dist/jena/binaries

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl --fail --location --retry 5 \
       "${JENA_DIST_BASE}/apache-jena-fuseki-${JENA_VERSION}.zip" \
       --output /tmp/fuseki.zip \
    && unzip -q /tmp/fuseki.zip -d /opt \
    && mv "/opt/apache-jena-fuseki-${JENA_VERSION}" /fuseki \
    && rm /tmp/fuseki.zip \
    && mkdir -p /fuseki/databases /fuseki/config

COPY aios-config.ttl /fuseki/config/aios-config.ttl

WORKDIR /fuseki
EXPOSE 3030

ENTRYPOINT ["/fuseki/fuseki-server"]
CMD ["--config=/fuseki/config/aios-config.ttl"]
