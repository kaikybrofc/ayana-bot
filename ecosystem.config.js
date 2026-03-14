module.exports = {
  apps: [
    {
      name: "ayana-bot",
      cwd: "/root/ayana-bot",
      script: "main.py",
      interpreter: "/root/ayana-bot/venv/bin/python",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 5000,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONUTF8: "1",
      },
      out_file: "/root/ayana-bot/logs/pm2-out.log",
      error_file: "/root/ayana-bot/logs/pm2-error.log",
      merge_logs: true,
      time: true,
    },
  ],
};
