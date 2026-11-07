FROM node:24-bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libaio1
COPY .npmrc package.json package-lock.json ./
ENV SEEKDB_BINDINGS_CACHE_DIR=/opt/seekdb-bindings
RUN npm ci --omit=dev && NODE_USE_ENV_PROXY=1 node -e '\
  const { ensureBindingsDownloaded } = require("./node_modules/@seekdb/js-bindings/download.js"); \
  ensureBindingsDownloaded().then(dir => { \
    require("node:fs").symlinkSync(`${dir}/libseekdb.so`, "/opt/libseekdb.so"); \
  }).catch(error => { console.error(error); process.exitCode = 1; });'
COPY case.json server.mjs ./
ENV HOST=0.0.0.0 PORT=4317 GAME_DB=/data
VOLUME /data
EXPOSE 4317
CMD ["sh", "-c", "LD_PRELOAD=/opt/libseekdb.so exec node server.mjs"]
