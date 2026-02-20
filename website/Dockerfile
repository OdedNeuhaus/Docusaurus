# Multi-stage Dockerfile for Docusaurus:
# 1) Build the static site with Node
# 2) Serve the built files with nginx

# ---- build stage ----
FROM node:20-alpine AS build
WORKDIR /app

# Install deps first (better caching)
COPY package.json package-lock.json* ./
RUN npm ci

# Copy site source and build it
COPY . .
RUN npm run build

# ---- runtime stage ----
FROM nginx:1.27-alpine

# Copy the static build output into nginx web root
COPY --from=build /app/build /usr/share/nginx/html

# Expose HTTP port
EXPOSE 80
