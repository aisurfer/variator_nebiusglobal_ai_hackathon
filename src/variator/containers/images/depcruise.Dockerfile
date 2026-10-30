# dependency-cruiser (MIT): module graph and cycles for JavaScript and TypeScript.
FROM node:22.11.0-alpine3.20
RUN npm install --global --no-fund --no-audit dependency-cruiser@18.7.0 typescript@5.6.3
ENTRYPOINT ["depcruise"]
