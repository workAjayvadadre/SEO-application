// pm2 process file:  pm2 start ecosystem.config.js
// API keys are read from .env in this folder (never commit it). nginx proxies to 127.0.0.1:PORT.
const path = require("path");

const PORT = process.env.KEYWORD_STUDIO_PORT || "8000";

module.exports = {
  apps: [
    {
      name: "keyword-studio",
      cwd: __dirname,
      script: path.join(__dirname, ".venv", "bin", "python"),
      args: ["server.py", "--host", "127.0.0.1", "--port", PORT],
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      env: {
        PYTHONUNBUFFERED: "1",
        // Domain(s) nginx serves this on, comma-separated, e.g. "seo-uat.indiraivf.in"
        ALLOWED_HOSTS: process.env.ALLOWED_HOSTS || "*",
      },
    },
  ],
};
