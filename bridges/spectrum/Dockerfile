FROM node:22-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --include=dev --no-audit --no-fund
COPY src/ src/
CMD ["./node_modules/.bin/tsx", "src/index.ts"]
