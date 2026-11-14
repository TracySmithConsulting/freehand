# FreeHand's WhatsApp bridge, run by Docker next to FreeHand (compose.yml
# service "whatsapp-bridge"), so it starts and stops with FreeHand on any
# computer. The linked-device session is NOT in the image: compose mounts
# it from freehand/bridge/auth_info_baileys.
FROM node:22.12-bookworm-slim

# git: one of Baileys' dependencies installs from a git repository.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY bridge-server.js whatsapp.js ./

ENV BRIDGE_IN_DOCKER=1 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8765 \
    WHATSAPP_AUTH_DIR=/data/auth_info_baileys
RUN mkdir -p /data/auth_info_baileys && chown -R node:node /data
USER node
EXPOSE 8765
CMD ["node", "bridge-server.js"]
