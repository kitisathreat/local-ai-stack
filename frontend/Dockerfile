FROM node:20-alpine AS build
WORKDIR /build

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --omit=optional --ignore-scripts || npm install --ignore-scripts

COPY frontend/ .
RUN npm run build

FROM nginx:alpine
COPY --from=build /build/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

# Default nginx image handles signals + healthchecks for us.
