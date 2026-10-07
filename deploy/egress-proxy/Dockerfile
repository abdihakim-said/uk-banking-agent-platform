# Squid as an egress allowlist proxy. Runs as the unprivileged squid user with a read-only root filesystem.
FROM alpine:3.20
RUN apk upgrade --no-cache && apk add --no-cache squid
COPY squid.conf /etc/squid/squid.conf
COPY allowed-domains.txt /etc/squid/allowlist/allowed-domains.txt
USER squid
EXPOSE 3128
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
