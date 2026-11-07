FROM node:24-bookworm-slim
WORKDIR /app
COPY .npmrc package.json package-lock.json ./
RUN npm ci --omit=dev
COPY case.json server.mjs ./
ENV HOST=0.0.0.0 PORT=4317 GAME_DB=/data
VOLUME /data
EXPOSE 4317
CMD ["node", "server.mjs"]
