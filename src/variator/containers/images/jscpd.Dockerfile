# jscpd 4 (MIT): copy/paste detection for 150+ languages. Version 4 keeps the documented JSON report.
FROM node:22.11.0-alpine3.20
RUN npm install --global --no-fund --no-audit jscpd@4.0.5
ENTRYPOINT ["jscpd"]
