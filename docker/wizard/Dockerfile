# The multi-step form fixture. No dependencies, so nothing to install: the standard library is
# the whole of it. Only ever started for the integration tests.
FROM node:22-alpine

WORKDIR /app
COPY server.js ./

ENV PORT=3200
EXPOSE 3200

CMD ["node", "server.js"]
