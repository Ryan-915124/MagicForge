# syntax=docker/dockerfile:1.7

ARG NODE_IMAGE=node:22.22.1-bookworm-slim

FROM ${NODE_IMAGE} AS dependencies

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /opt/magicforge/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund


FROM dependencies AS build

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

COPY frontend/components.json ./components.json
COPY frontend/next-env.d.ts ./next-env.d.ts
COPY frontend/next.config.ts ./next.config.ts
COPY frontend/postcss.config.mjs ./postcss.config.mjs
COPY frontend/tsconfig.json ./tsconfig.json
COPY frontend/src ./src
RUN npm run build


FROM ${NODE_IMAGE} AS runtime

ENV HOME=/home/node \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

WORKDIR /opt/magicforge/frontend
COPY --from=build --chown=node:node /opt/magicforge/frontend/package.json ./package.json
COPY --from=build --chown=node:node /opt/magicforge/frontend/package-lock.json ./package-lock.json
COPY --from=build --chown=node:node /opt/magicforge/frontend/next.config.ts ./next.config.ts
COPY --from=build --chown=node:node /opt/magicforge/frontend/node_modules ./node_modules
COPY --from=build --chown=node:node /opt/magicforge/frontend/.next ./.next

USER node
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD node -e "fetch('http://127.0.0.1:3000/login').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]


FROM dependencies AS development

ENV HOME=/home/node \
    NODE_ENV=development \
    NEXT_TELEMETRY_DISABLED=1

COPY --chown=node:node frontend/components.json ./components.json
COPY --chown=node:node frontend/next-env.d.ts ./next-env.d.ts
COPY --chown=node:node frontend/next.config.ts ./next.config.ts
COPY --chown=node:node frontend/postcss.config.mjs ./postcss.config.mjs
COPY --chown=node:node frontend/tsconfig.json ./tsconfig.json
COPY --chown=node:node frontend/src ./src
RUN mkdir -p .next && chown node:node .next

USER node
EXPOSE 3000
CMD ["npm", "run", "dev"]
