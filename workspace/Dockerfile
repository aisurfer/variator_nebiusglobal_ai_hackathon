# The workspace (Express + React), built with the build context workspace/. Serves the UI and /api on PORT (3002).
# KB_URL and VARIATOR_API_URL must point at the kb-api and variator-api services (see README.md).
FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:24-bookworm-slim AS runtime
ENV NODE_ENV=production \
    PORT=3002
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build --chown=node:node /app/dist ./dist
COPY --from=build --chown=node:node /app/server ./server
COPY --from=build --chown=node:node /app/src ./src
COPY --from=build --chown=node:node /app/config ./config
# the decisions volume (DECISIONS_DIR): a new named volume takes this owner, so the node user can write it
RUN mkdir -p /data/decisions && chown node:node /data/decisions
USER node
EXPOSE 3002
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3002)+'/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
CMD ["npm", "start"]
