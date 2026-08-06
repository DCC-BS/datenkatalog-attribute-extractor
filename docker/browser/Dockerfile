# The browser we own, because everything general about reading a web form needs one.
#
# Firecrawl cannot serve this role: self-hosted it returns HTML only — no screenshot, no
# geometry, no computed styles — so every question about what the page *looks like* had to be
# answered by guessing from the DOM. Guessing from the DOM is what made the previous
# extraction fit one form and no others.
#
# The image ships Chromium and its system libraries already; only the playwright npm package
# is installed here, pinned to the image's own version so client and browser cannot drift.
FROM mcr.microsoft.com/playwright:v1.59.1-noble

WORKDIR /srv

RUN npm install --no-save playwright@1.59.1

COPY package.json server.js observe.js ./

USER pwuser

EXPOSE 3100

CMD ["node", "server.js"]
